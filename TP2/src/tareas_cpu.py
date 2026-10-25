import hashlib


def hash_paso(estado: bytes, iteraciones: int) -> bytes:
    """Aplica SHA-256 `iteraciones` veces, cada vez sobre el resultado
    anterior, y devuelve el resultado final (en bytes). Es CPU-bound puro:
    no hay ninguna espera, el procesador no para.

    El hash iterado se puede cortar en pedazos: hacer 1.000.000 de vueltas es
    lo mismo que hacer 500.000, tomar el resultado y hacer otras 500.000. Esa
    propiedad es la que nos permite cancelar el trabajo entre pedazo y pedazo
    (ver Plataforma._hashear).

    Vive en su propio módulo, a nivel raíz y sin depender de `self` ni de
    variables globales del programa, porque un ProcessPoolExecutor tiene que
    poder serializar (pickle) la función para mandarla a otro proceso.
    Un método de clase o una lambda no se pueden mandar así.
    """
    h = estado
    for _ in range(iteraciones):
        h = hashlib.sha256(h).digest()
    return h


def hash_iterado(texto: str, iteraciones: int) -> str:
    """Hash completo de una sola vez (lo usa el modo 'bloqueante')."""
    return hash_paso(texto.encode(), iteraciones).hex()