import asyncio
import logging
import os
import time
import uuid

import httpx

from modelos import Estado

log = logging.getLogger("plataforma")

# Cantidad de workers configurable por variable de entorno (default 3).
NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "3"))


class Plataforma:
    """Dueña del estado: el dict de tareas, la cola y el pool de workers.

    Todo esto corre en UN solo hilo (el del event loop). Las corrutinas solo
    se interrumpen en un `await`, así que mientras una corrutina modifica el
    dict sin hacer `await` en el medio, ninguna otra se mete. Por eso no hay
    Lock en este archivo.
    """

    def __init__(self, num_workers: int):
        self.num_workers = num_workers
        self.tareas: dict[str, dict] = {}
        self.cola: asyncio.Queue | None = None
        self.workers: list[asyncio.Task] = []
        self.ocupados = 0

    # ---------- ciclo de vida ----------

    def iniciar(self):
        # La cola se crea acá y no en __init__ para que nazca dentro del
        # event loop que la va a usar.
        self.cola = asyncio.Queue()
        self.workers = [
            asyncio.create_task(self._worker(), name=f"worker-{i}")
            for i in range(self.num_workers)
        ]
        log.info("Pool iniciado con %d workers", self.num_workers)

    async def detener(self):
        # Etapa 2: apagado bruto, cancela los workers sin esperar a nadie.
        # El apagado ordenado (esperar tareas en curso, volcar JSON) es la
        # etapa 6.
        for w in self.workers:
            w.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    # ---------- API hacia main.py ----------

    def encolar(self, tipo: str, parametros: dict, timeout_s: float) -> dict:
        id_tarea = str(uuid.uuid4())
        tarea = {
            "id": id_tarea,
            "tipo": tipo,
            "parametros": parametros,
            "timeout_s": timeout_s,  # se guarda, pero todavía no se aplica
            "estado": Estado.PENDIENTE,
            "resultado": None,
            "error": None,
            "creada_en": time.time(),
            "inicio_en": None,
            "fin_en": None,
        }
        self.tareas[id_tarea] = tarea
        self.cola.put_nowait(id_tarea)  # no bloquea: la cola no tiene límite
        return tarea

    def estadisticas(self) -> dict:
        por_estado = {e.value: 0 for e in Estado}
        duraciones = []
        for t in self.tareas.values():
            por_estado[t["estado"].value] += 1
            if t["estado"] == Estado.COMPLETADA:
                duraciones.append(t["fin_en"] - t["inicio_en"])
        promedio = sum(duraciones) / len(duraciones) if duraciones else None
        return {
            "total": len(self.tareas),
            "por_estado": por_estado,
            "tiempo_promedio_s": promedio,
            "workers_total": self.num_workers,
            "workers_ocupados": self.ocupados,
            "en_cola": self.cola.qsize(),
        }

    # ---------- workers ----------

    async def _worker(self):
        while True:
            id_tarea = await self.cola.get()
            try:
                await self._procesar(self.tareas[id_tarea])
            except Exception:
                # Red de seguridad: si hay un bug en NUESTRO código de
                # bookkeeping, lo registramos pero el worker sigue vivo.
                # Sin esto, una excepción mataría esta corrutina en
                # silencio y el pool quedaría con un worker menos.
                log.exception("Error inesperado procesando %s", id_tarea)
            finally:
                self.cola.task_done()

    async def _procesar(self, tarea: dict):
        if tarea["estado"] != Estado.PENDIENTE:
            return  # la cancelaron mientras esperaba en la cola

        self.ocupados += 1
        tarea["estado"] = Estado.EJECUTANDO
        tarea["inicio_en"] = time.time()
        try:
            tarea["resultado"] = await self._ejecutar(tarea)
            tarea["estado"] = Estado.COMPLETADA
        except Exception as e:
            # Una tarea que falla NO tiene que tumbar al worker: la marcamos
            # fallida con el motivo y el worker pasa a la siguiente.
            tarea["estado"] = Estado.FALLIDA
            tarea["error"] = f"{type(e).__name__}: {e}"
        finally:
            tarea["fin_en"] = time.time()
            self.ocupados -= 1

    async def _ejecutar(self, tarea: dict):
        if tarea["tipo"] == "esperar":
            segundos = tarea["parametros"]["segundos"]
            # asyncio.sleep CEDE el control al event loop mientras espera.
            # time.sleep acá congelaría la API entera.
            await asyncio.sleep(segundos)
            return {"esperado_s": segundos}
        if tarea["tipo"] == "descargar":
            return await self._descargar(tarea["parametros"])
        raise NotImplementedError(
            f"el tipo '{tarea['tipo']}' todavía no está implementado"
        )

    # ---------- tipos de tarea ----------

    async def _descargar(self, parametros: dict) -> dict:
        urls = parametros["urls"]
        secuencial = parametros.get("secuencial", False)
        t0 = time.perf_counter()

        # Un solo cliente para todas las URLs: comparte el pool de
        # conexiones. El timeout de 10 s por request es PROVISORIO: evita que
        # un servidor colgado retenga al worker para siempre. El timeout real
        # de la tarea (timeout_s) se aplica en la etapa 5.
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as cliente:
            if secuencial:
                # Cada await espera a que termine la descarga ANTERIOR
                # antes de arrancar la siguiente: tarda la SUMA de todas.
                descargas = []
                for u in urls:
                    descargas.append(await self._bajar(cliente, u))
            else:
                # gather arranca todas las corrutinas a la vez y espera a
                # que terminen todas: tarda lo que tarde la MÁS LENTA.
                descargas = await asyncio.gather(
                    *(self._bajar(cliente, u) for u in urls)
                )

        exitosas = sum(1 for d in descargas if d["ok"])
        if exitosas == 0:
            # Si no se pudo bajar NINGUNA, la tarea falla. Si se bajó al
            # menos una, se considera completada y el detalle por URL
            # muestra cuáles fallaron.
            raise RuntimeError(
                f"no se pudo descargar ninguna de las {len(urls)} URLs: "
                f"{descargas[0]['error']}"
            )
        return {
            "modo": "secuencial" if secuencial else "concurrente",
            "total_urls": len(urls),
            "exitosas": exitosas,
            "fallidas": len(urls) - exitosas,
            "tiempo_total_s": round(time.perf_counter() - t0, 3),
            "descargas": descargas,
        }

    @staticmethod
    async def _bajar(cliente: httpx.AsyncClient, url: str) -> dict:
        """Descarga UNA url. Nunca lanza HTTPError: lo convierte en datos,
        así un fallo no cancela al resto dentro del gather.
        'ok' significa que llegó una respuesta HTTP (aunque sea un 404);
        'ok': False significa error de red (DNS, conexión, timeout...)."""
        t0 = time.perf_counter()
        try:
            r = await cliente.get(url)
            return {
                "url": url,
                "ok": True,
                "status": r.status_code,
                "bytes": len(r.content),
                "tiempo_s": round(time.perf_counter() - t0, 3),
            }
        except httpx.HTTPError as e:
            return {
                "url": url,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "tiempo_s": round(time.perf_counter() - t0, 3),
            }