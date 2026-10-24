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
        # Validamos 'esperar' y 'descargar'. Los parámetros de 'hashear' se
        # validan cuando lo implementemos (etapa 4).
        if self.tipo == "esperar":
            s = self.parametros.get("segundos")
            if isinstance(s, bool) or not isinstance(s, (int, float)) or s < 0:
                raise ValueError(
                    "'esperar' requiere parametros.segundos: un número >= 0"
                )
        elif self.tipo == "descargar":
            urls = self.parametros.get("urls")
            if (
                not isinstance(urls, list)
                or not urls
                or not all(
                    isinstance(u, str) and u.startswith(("http://", "https://"))
                    for u in urls
                )
            ):
                raise ValueError(
                    "'descargar' requiere parametros.urls: una lista no vacía "
                    "de URLs que empiecen con http:// o https://"
                )
            # 'secuencial' es opcional: existe solo para poder medir la
            # diferencia contra la versión concurrente (medición 1).
            if not isinstance(self.parametros.get("secuencial", False), bool):
                raise ValueError("parametros.secuencial debe ser true o false")
        return self