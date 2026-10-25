import hashlib


def hash_iterado(texto: str, iteraciones: int) -> str:
    """SHA-256 aplicado `iteraciones` veces, cada vez sobre el resultado
    anterior. Es CPU-bound puro: no hay ninguna espera, el procesador no para.

    Vive en su propio módulo, a nivel raíz y sin depender de `self` ni de
    variables globales del programa, porque un ProcessPoolExecutor tiene que
    poder serializar (pickle) la función para mandarla a otro proceso.
    Un método de clase o una lambda no se pueden mandar así.
    """
    h = texto.encode()
    for _ in range(iteraciones):
        h = hashlib.sha256(h).digest()
    return h.hex()