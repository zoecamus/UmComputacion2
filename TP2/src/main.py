import time
import uuid
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException

from modelos import Estado, TareaCrear

app = FastAPI(title="Plataforma de Tareas Distribuidas")

# Etapa 1: todo el estado es este dict, en memoria. Todavía no hay cola ni
# workers, así que ninguna tarea se ejecuta: solo se registran.
tareas: dict[str, dict] = {}


@app.post("/tareas", status_code=201)
async def crear_tarea(datos: TareaCrear):
    id_tarea = str(uuid.uuid4())
    tareas[id_tarea] = {
        "id": id_tarea,
        "tipo": datos.tipo,
        "parametros": datos.parametros,
        "timeout_s": datos.timeout_s,
        "estado": Estado.PENDIENTE,
        "resultado": None,
        "error": None,
        "creada_en": time.time(),
    }
    return tareas[id_tarea]


@app.get("/tareas")
async def listar_tareas(estado: Optional[Estado] = None):
    # `estado` es un query param opcional: GET /tareas?estado=pendiente
    # Si mandan un valor que no está en el Enum, FastAPI responde 422.
    todas = list(tareas.values())
    if estado is not None:
        todas = [t for t in todas if t["estado"] == estado]
    return todas


@app.get("/tareas/{id_tarea}")
async def obtener_tarea(id_tarea: str):
    if id_tarea not in tareas:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return tareas[id_tarea]


@app.delete("/tareas/{id_tarea}")
async def cancelar_tarea(id_tarea: str):
    # Placeholder de la etapa 1: solo cambia el estado en el dict.
    # La cancelación real (de una corrutina en ejecución) es la etapa 5.
    if id_tarea not in tareas:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    if tareas[id_tarea]["estado"] in (Estado.PENDIENTE, Estado.EJECUTANDO):
        tareas[id_tarea]["estado"] = Estado.CANCELADA
    return tareas[id_tarea]


@app.get("/estadisticas")
async def estadisticas():
    por_estado = {e.value: 0 for e in Estado}
    for t in tareas.values():
        por_estado[t["estado"].value] += 1
    return {
        "total": len(tareas),
        "por_estado": por_estado,
        # Datos falsos por ahora: no hay workers ni tareas terminadas.
        "tiempo_promedio_s": None,
        "workers_ocupados": 0,
    }


if __name__ == "__main__":
    # host 0.0.0.0 y no localhost: dentro del contenedor, "localhost" es el
    # propio contenedor, y Docker no podría reenviar el puerto hacia el host.
    uvicorn.run(app, host="0.0.0.0", port=8000)