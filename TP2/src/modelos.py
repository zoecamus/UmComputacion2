from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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

    @model_validator(mode="after")
    def validar_parametros(self):
        # Etapa 2: solo validamos 'esperar'. Los parámetros de 'descargar' y
        # 'hashear' se validan cuando implementemos esos tipos.
        if self.tipo == "esperar":
            s = self.parametros.get("segundos")
            if isinstance(s, bool) or not isinstance(s, (int, float)) or s < 0:
                raise ValueError(
                    "'esperar' requiere parametros.segundos: un número >= 0"
                )
        return self