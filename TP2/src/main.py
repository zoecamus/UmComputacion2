import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException

from modelos import Estado, TareaCrear
from plataforma import NUM_WORKERS, Plataforma

logging.basicConfig(level=logging.INFO)

plataforma = Plataforma(NUM_WORKERS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lo que está antes del yield corre al ARRANCAR el servidor; lo de
    # después, al apagarlo. Los workers tienen que nacer acá adentro porque
    # necesitan el event loop de uvicorn ya andando.
    plataforma.iniciar()
    yield
    await plataforma.detener()


app = FastAPI(title="Plataforma de Tareas Distribuidas", lifespan=lifespan)


@app.post("/tareas", status_code=201)
async def crear_tarea(datos: TareaCrear):
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
    tarea = plataforma.tareas[id_tarea]

    if tarea["estado"] == Estado.PENDIENTE:
        # Sigue en la cola: el worker la va a saltear cuando la saque.
        tarea["estado"] = Estado.CANCELADA
    elif tarea["estado"] == Estado.EJECUTANDO:
        # Cancelar una corrutina en ejecución es la etapa 5. Hasta entonces
        # lo decimos explícitamente en vez de fingir que anduvo.
        raise HTTPException(
            status_code=409,
            detail="Cancelar una tarea en ejecución todavía no está implementado",
        )
    return tarea


@app.get("/estadisticas")
async def estadisticas():
    return plataforma.estadisticas()


if __name__ == "__main__":
    # host 0.0.0.0 y no localhost: dentro del contenedor, "localhost" es el
    # propio contenedor, y Docker no podría reenviar el puerto hacia el host.
    uvicorn.run(app, host="0.0.0.0", port=8000)