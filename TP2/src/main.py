import asyncio
import logging
import signal
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException

from modelos import Estado, TareaCrear
from plataforma import NUM_WORKERS, SHUTDOWN_TIMEOUT_S, Plataforma

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

plataforma = Plataforma(NUM_WORKERS)

# El objeto uvicorn.Server se crea al final del archivo (bloque __main__) y
# se guarda acá para poder pedirle que termine cuando acabó nuestro apagado.
server: uvicorn.Server | None = None
_tarea_apagado: asyncio.Task | None = None  # guardada para que no la recolecte el GC


def _instalar_senales():
    """Registra SIGTERM y SIGINT en el event loop.

    loop.add_signal_handler (solo Unix) ejecuta nuestra función DENTRO del
    event loop, no en un contexto especial de señales: por eso desde ahí se
    pueden crear tasks y tocar el estado sin riesgos. Se instala acá, después
    de que uvicorn puso los suyos, así que los reemplaza."""
    if server is None:
        log.info("Sin uvicorn.Server propio: queda el manejo de señales de uvicorn")
        return
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _al_recibir_senal, sig)
    except NotImplementedError:  # Windows
        log.warning("add_signal_handler no existe en esta plataforma; "
                    "queda el manejo de señales de uvicorn")


def _al_recibir_senal(sig: int):
    global _tarea_apagado
    nombre = signal.Signals(sig).name
    if _tarea_apagado is None:
        log.info("Recibí %s", nombre)
        _tarea_apagado = asyncio.create_task(_apagar(nombre))
    else:
        # Segunda señal mientras esperábamos: el usuario insiste. No se
        # espera más, pero igual se cancelan las tareas y se vuelca el estado.
        log.warning("Segunda señal (%s): dejo de esperar a las tareas", nombre)
        plataforma.dejar_de_esperar()


async def _apagar(motivo: str):
    try:
        await plataforma.apagar(SHUTDOWN_TIMEOUT_S, motivo)
    except Exception:
        log.exception("Falló el apagado ordenado")
    finally:
        # Pase lo que pase, le pedimos a uvicorn que termine; si no, un
        # error en el volcado dejaría al servidor colgado para siempre.
        server.should_exit = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lo que está antes del yield corre al ARRANCAR el servidor; lo de
    # después, al apagarlo. Los workers tienen que nacer acá adentro porque
    # necesitan el event loop de uvicorn ya andando.
    plataforma.iniciar()
    _instalar_senales()
    yield
    # Si el apagado vino por nuestro handler, apagar() ya corrió y esto no
    # hace nada. Si uvicorn terminó por otro camino, esto garantiza el volcado.
    await plataforma.apagar(SHUTDOWN_TIMEOUT_S, "apagado de uvicorn")
    await plataforma.detener()


app = FastAPI(title="Plataforma de Tareas Distribuidas", lifespan=lifespan)


@app.post("/tareas", status_code=201)
async def crear_tarea(datos: TareaCrear):
    if plataforma.cerrando:
        # Durante el apagado seguimos atendiendo (se puede consultar cómo
        # van las tareas), pero no aceptamos trabajo nuevo.
        raise HTTPException(
            status_code=503,
            detail="El servidor se está apagando: no se aceptan tareas nuevas",
        )
    # Solo registra y encola: responde al instante, sin esperar a que la
    # tarea se ejecute.
    return plataforma.encolar(datos.tipo, datos.parametros, datos.timeout_s)


@app.get("/tareas")
async def listar_tareas(estado: Optional[Estado] = None):
    # `estado` es un query param opcional: GET /tareas?estado=pendiente
    # Si mandan un valor que no está en el Enum, FastAPI responde 422.
    todas = list(plataforma.tareas.values())
    if estado is not None:
        todas = [t for t in todas if t["estado"] == estado]
    return todas


@app.get("/tareas/{id_tarea}")
async def obtener_tarea(id_tarea: str):
    if id_tarea not in plataforma.tareas:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return plataforma.tareas[id_tarea]


@app.delete("/tareas/{id_tarea}")
async def cancelar_tarea(id_tarea: str):
    if id_tarea not in plataforma.tareas:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    # Pendiente o en ejecución: se cancela. Ya terminada: se devuelve igual.
    return plataforma.cancelar(id_tarea)


@app.get("/estadisticas")
async def estadisticas():
    return plataforma.estadisticas()


if __name__ == "__main__":
    # host 0.0.0.0 y no localhost: dentro del contenedor, "localhost" es el
    # propio contenedor, y Docker no podría reenviar el puerto hacia el host.
    #
    # Armamos el Server a mano (en vez de uvicorn.run) para tener el objeto y
    # poder pedirle que termine con server.should_exit = True.
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000))
    server.run()