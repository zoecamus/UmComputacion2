import asyncio
import glob
import json
import logging
import os
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import httpx

from modelos import Estado
from tareas_cpu import hash_iterado, hash_paso, ignorar_sigint

log = logging.getLogger("plataforma")

# Cantidad de workers configurable por variable de entorno (default 3).
NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "3"))

# Cuánto esperamos a las tareas en curso al apagar. Tiene que ser MENOR que
# el stop_grace_period de docker-compose (30 s): si no, Docker manda SIGKILL
# antes de que alcancemos a volcar el estado.
SHUTDOWN_TIMEOUT_S = float(os.environ.get("SHUTDOWN_TIMEOUT_S", "20"))

# Carpeta donde se escriben (y se buscan al arrancar) los volcados. En Docker
# es un volumen, para que sobrevivan a que el contenedor se recree.
DIR_VOLCADOS = os.environ.get("DIR_VOLCADOS", "datos")

# Iteraciones de hash por cada viaje al executor (~0,2 s en una máquina
# normal). Más chico = se cancela más rápido pero hay más viajes entre
# procesos; más grande = al revés.
PASO_HASH = 500_000


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
        self.executor: ProcessPoolExecutor | None = None
        # id -> asyncio.Task del trabajo que se está ejecutando AHORA.
        self.en_curso: dict[str, asyncio.Task] = {}
        # ids que recibieron un DELETE mientras se ejecutaban.
        self._cancelando: set[str] = set()
        # Apagado: True desde que llega la señal. Con esto en True no se
        # aceptan tareas nuevas ni los workers arrancan las que quedan en cola.
        self.cerrando = False
        self._apagando = False  # para que apagar() corra una sola vez
        # Se "prende" cuando NO hay ninguna tarea ejecutándose. Permite
        # esperar "hasta que se libere el pool" sin dar vueltas preguntando.
        self._libres: asyncio.Event | None = None

    # ---------- ciclo de vida ----------

    def iniciar(self):
        # La cola se crea acá y no en __init__ para que nazca dentro del
        # event loop que la va a usar.
        self.cola = asyncio.Queue()
        self._libres = asyncio.Event()
        self._libres.set()  # al arrancar no hay nada ejecutándose
        # Procesos separados para las tareas CPU-bound. Un proceso por
        # worker: así, si todos los workers hashean a la vez, cada uno tiene
        # su proceso. Los procesos se crean recién al primer uso.
        # `initializer` hace que los hijos ignoren Ctrl+C (ver tareas_cpu).
        self.executor = ProcessPoolExecutor(
            max_workers=self.num_workers, initializer=ignorar_sigint
        )
        self.workers = [
            asyncio.create_task(self._worker(), name=f"worker-{i}")
            for i in range(self.num_workers)
        ]
        log.info("Pool iniciado con %d workers", self.num_workers)
        self._reportar_volcado_previo()

    async def apagar(self, limite_s: float, motivo: str = "apagado"):
        """Apagado ORDENADO. Se puede llamar más de una vez: solo la primera
        hace algo.

        1. Deja de aceptar tareas nuevas (cerrando = True).
        2. Espera, hasta `limite_s`, a que terminen las que están corriendo.
        3. Las que no terminaron las cancela y las deja como 'cancelada'.
        4. Vuelca el estado a estado_<timestamp>.json.

        Las tareas que seguían 'pendiente' en la cola NO se ejecutan: quedan
        'pendiente' en el volcado, que es lo que realmente pasó con ellas."""
        if self._apagando:
            return
        self._apagando = True
        self.cerrando = True
        log.info(
            "Apagado ordenado (%s): %d en ejecución, %d esperando en cola. "
            "Espero hasta %.0f s.",
            motivo, self.ocupados, self.cola.qsize(), limite_s,
        )

        try:
            async with asyncio.timeout(limite_s):
                await self._libres.wait()
        except TimeoutError:
            pass  # se acabó el límite; abajo cancelamos lo que quede

        if self.ocupados > 0:
            # Cancelamos con el mismo mecanismo que DELETE (cancelar()): la
            # tarea queda 'cancelada' y el worker sigue sano. Después
            # anotamos el motivo real.
            restantes = list(self.en_curso)
            log.warning(
                "Cancelo %d tarea(s) que no terminaron a tiempo", len(restantes)
            )
            for id_tarea in restantes:
                self.cancelar(id_tarea)
                self.tareas[id_tarea]["error"] = (
                    "interrumpida por el apagado del servidor"
                )
            # Damos un respiro corto para que cada cancelación termine de
            # limpiar (cerrar conexiones, etc.) antes de volcar el estado.
            # clear() porque dejar_de_esperar() pudo haber dejado el Event
            # prendido aunque todavía haya tareas; sin esto la espera de
            # abajo no esperaría nada. No hay `await` entre el for y este
            # clear(), así que ningún worker pudo terminar en el medio.
            self._libres.clear()
            try:
                async with asyncio.timeout(5):
                    await self._libres.wait()
            except TimeoutError:
                log.error("Quedaron tareas sin terminar de cancelarse")

        self._volcar_estado(motivo)

    def dejar_de_esperar(self):
        """Segunda señal durante el apagado: no esperar más. Despierta a
        apagar() aunque haya tareas corriendo; ahí las cancela y vuelca."""
        if self._libres is not None:
            self._libres.set()

    async def detener(self):
        # Se llama al final, cuando apagar() ya hizo lo importante. Acá solo
        # se cancelan los workers (que a esta altura están ociosos esperando
        # en cola.get()) y se libera el pool de procesos.
        for w in self.workers:
            w.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.executor.shutdown(wait=False, cancel_futures=True)

    # ---------- persistencia ----------

    def _volcar_estado(self, motivo: str):
        ahora = datetime.now(timezone.utc)
        nombre = f"estado_{ahora.strftime('%Y%m%d_%H%M%S')}.json"
        os.makedirs(DIR_VOLCADOS, exist_ok=True)
        ruta = os.path.join(DIR_VOLCADOS, nombre)
        contenido = {
            "generado_en": ahora.isoformat(),
            "motivo": motivo,
            "estadisticas": self.estadisticas(),
            "tareas": list(self.tareas.values()),
        }
        # Se escribe a un archivo temporal y después se renombra. os.replace
        # es atómico: si nos matan (SIGKILL) a mitad de la escritura, queda
        # un .tmp sin terminar, nunca un estado_*.json cortado por la mitad.
        # Escribir un archivo chico es bloqueante, pero tarda milisegundos y
        # a esta altura no hay nadie más a quien atender.
        tmp = ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(contenido, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ruta)
        log.info("Estado volcado a %s (%d tareas)", ruta, len(self.tareas))

    def _reportar_volcado_previo(self):
        """Al arrancar: si hay un volcado de una ejecución anterior, solo lo
        informamos por log. NO se cargan las tareas de vuelta (limitación
        conocida, ver README)."""
        archivos = sorted(glob.glob(os.path.join(DIR_VOLCADOS, "estado_*.json")))
        if not archivos:
            log.info("No hay volcados previos en %s/", DIR_VOLCADOS)
            return
        ultimo = archivos[-1]
        try:
            with open(ultimo, encoding="utf-8") as f:
                datos = json.load(f)
            est = datos["estadisticas"]
            log.info(
                "Volcado previo encontrado: %s (generado %s por %s): "
                "%d tareas, por estado %s. Hay %d volcado(s) en total.",
                ultimo, datos["generado_en"], datos["motivo"],
                est["total"], est["por_estado"], len(archivos),
            )
        except (OSError, ValueError, KeyError) as e:
            log.warning("Hay un volcado previo (%s) pero no se pudo leer: %s", ultimo, e)

    # ---------- API hacia main.py ----------

    def encolar(self, tipo: str, parametros: dict, timeout_s: float) -> dict:
        id_tarea = str(uuid.uuid4())
        tarea = {
            "id": id_tarea,
            "tipo": tipo,
            "parametros": parametros,
            "timeout_s": timeout_s,
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
            "apagando": self.cerrando,
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
        if self.cerrando:
            return  # nos estamos apagando: queda 'pendiente', no se ejecuta

        id_tarea = tarea["id"]
        self.ocupados += 1
        self._libres.clear()
        tarea["estado"] = Estado.EJECUTANDO
        tarea["inicio_en"] = time.time()

        # El trabajo real corre en SU PROPIA asyncio.Task, separada del
        # worker. Así se puede cancelar el trabajo (trabajo.cancel()) sin
        # matar al worker, que tiene que seguir vivo para la próxima tarea.
        # No hay ningún `await` entre cambiar el estado a EJECUTANDO y
        # guardar la task en en_curso, así que cancelar() nunca ve un estado
        # EJECUTANDO sin su task.
        trabajo = asyncio.create_task(self._ejecutar(tarea), name=f"tarea-{id_tarea[:8]}")
        self.en_curso[id_tarea] = trabajo
        try:
            # El timeout cuenta desde que la tarea EMPIEZA a ejecutarse, no
            # desde que se encoló. Al vencer, asyncio.timeout cancela esta
            # corrutina (el worker), esa cancelación se propaga a `trabajo`
            # (que es lo que está esperando) y el `with` la convierte en un
            # TimeoutError.
            async with asyncio.timeout(tarea["timeout_s"]):
                tarea["resultado"] = await trabajo
            tarea["estado"] = Estado.COMPLETADA
        except TimeoutError:
            tarea["estado"] = Estado.FALLIDA
            tarea["error"] = f"TimeoutError: superó el timeout de {tarea['timeout_s']} s"
        except asyncio.CancelledError:
            # Hay DOS motivos posibles para llegar acá y hay que distinguirlos:
            #  - alguien hizo DELETE: cancelar() canceló `trabajo`. El worker
            #    NO fue cancelado, sigue vivo.
            #  - se está apagando el servidor: detener() canceló al worker.
            # CancelledError hereda de BaseException, así que `except
            # Exception` no la agarra; hay que pedirla explícitamente.
            if id_tarea in self._cancelando:
                tarea["estado"] = Estado.CANCELADA
            else:
                raise  # apagado: dejamos que el worker muera (ver finally)
        except Exception as e:
            # Una tarea que falla NO tiene que tumbar al worker: la marcamos
            # fallida con el motivo y el worker pasa a la siguiente.
            tarea["estado"] = Estado.FALLIDA
            tarea["error"] = f"{type(e).__name__}: {e}"
        finally:
            self.en_curso.pop(id_tarea, None)
            self._cancelando.discard(id_tarea)
            tarea["fin_en"] = time.time()
            self.ocupados -= 1
            if self.ocupados == 0:
                self._libres.set()  # despierta a apagar() si está esperando

    def cancelar(self, id_tarea: str) -> dict:
        """Cancela una tarea según su estado. Es síncrona (sin await): el
        cambio de estado y el cancel() ocurren juntos, sin que el event loop
        pueda meter a otra corrutina en el medio."""
        tarea = self.tareas[id_tarea]
        if tarea["estado"] == Estado.PENDIENTE:
            # Sigue en la cola: el worker la va a saltear cuando la saque.
            tarea["estado"] = Estado.CANCELADA
            tarea["fin_en"] = time.time()
        elif tarea["estado"] == Estado.EJECUTANDO:
            # Marcamos ANTES de cancelar para que _procesar sepa que fue un
            # DELETE y no un apagado. Después task.cancel() inyecta
            # CancelledError en el punto donde `trabajo` está esperando
            # (un sleep, una request de red, un run_in_executor...).
            self._cancelando.add(id_tarea)
            tarea["estado"] = Estado.CANCELADA
            tarea["fin_en"] = time.time()
            self.en_curso[id_tarea].cancel()
        # Si ya terminó (completada, fallida o cancelada), no hay nada que
        # hacer: se devuelve tal cual.
        return tarea

    async def _ejecutar(self, tarea: dict):
        if tarea["tipo"] == "esperar":
            segundos = tarea["parametros"]["segundos"]
            # asyncio.sleep CEDE el control al event loop mientras espera.
            # time.sleep acá congelaría la API entera.
            await asyncio.sleep(segundos)
            return {"esperado_s": segundos}
        if tarea["tipo"] == "descargar":
            return await self._descargar(tarea["parametros"])
        if tarea["tipo"] == "hashear":
            return await self._hashear(tarea["parametros"])
        raise NotImplementedError(
            f"el tipo '{tarea['tipo']}' todavía no está implementado"
        )

    # ---------- tipos de tarea ----------

    async def _hashear(self, parametros: dict) -> dict:
        texto = parametros["texto"]
        n = parametros["iteraciones"]
        bloqueante = parametros.get("bloqueante", False)
        t0 = time.perf_counter()

        if bloqueante:
            # MAL A PROPÓSITO (solo para medir): el hash corre en el hilo del
            # event loop y no hay ningún `await` en el medio. Mientras dura,
            # el loop no atiende a nadie: ni la API ni los otros workers.
            resultado = hash_iterado(texto, n)
        else:
            # El trabajo va a otro PROCESO y se hace `await` del resultado:
            # el event loop queda libre mientras tanto.
            #
            # Se manda en PEDAZOS de PASO_HASH iteraciones, no todo junto.
            # Motivo: cancelar el `await` de run_in_executor NO detiene al
            # proceso hijo si ya está calculando; seguiría quemando CPU hasta
            # terminar. Entre pedazo y pedazo hay un `await`, que es un punto
            # donde la cancelación y el timeout pueden entrar. Lo máximo que
            # se desperdicia al cancelar es UN pedazo (~0,2 s).
            loop = asyncio.get_running_loop()
            estado = texto.encode()
            restantes = n
            while restantes > 0:
                paso = min(PASO_HASH, restantes)
                estado = await loop.run_in_executor(
                    self.executor, hash_paso, estado, paso
                )
                restantes -= paso
            resultado = estado.hex()

        return {
            "modo": "bloqueante" if bloqueante else "executor",
            "iteraciones": n,
            "hash": resultado,
            "tiempo_s": round(time.perf_counter() - t0, 3),
        }

    async def _descargar(self, parametros: dict) -> dict:
        urls = parametros["urls"]
        secuencial = parametros.get("secuencial", False)
        t0 = time.perf_counter()

        # Un solo cliente para todas las URLs: comparte el pool de
        # conexiones. El timeout de 10 s es POR REQUEST (red); el timeout de
        # la tarea completa (timeout_s) lo aplica _procesar. Si se cancela la
        # tarea, el `async with` cierra el cliente y sus conexiones solo.
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