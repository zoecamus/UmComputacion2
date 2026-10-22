from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Estado(str, Enum):
    """Los cinco estados que pide la consigna."""
    PENDIENTE = "pendiente"
    EJECUTANDO = "ejecutando"
    COMPLETADA = "completada"
    FALLIDA = "fallida"
    CANCELADA = "cancelada"


class TareaCrear(BaseModel):
    """Cuerpo que esperamos en POST /tareas. Pydantic lo valida solo:
    si falta un campo o el tipo no es uno de los tres, FastAPI responde 422
    sin que escribamos una sola línea de validación."""
    tipo: Literal["descargar", "hashear", "esperar"]
    parametros: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(default=30.0, gt=0)