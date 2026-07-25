"""
Agregador: no es un proceso propio en esta versión reducida, es el DUEÑO
del diccionario compartido. Lo crea main.py con un multiprocessing.Manager()
y se lo pasa a cada analizador y al display.

Por qué Manager.dict() y no un dict común:
Un dict de Python vive en la memoria privada de UN proceso. Después de un
fork(), cada proceso hijo tiene su PROPIA copia (copy-on-write) de ese dict:
si el analizador le escribe algo, el padre o el display NUNCA lo ven,
porque están mirando copias distintas en memoria física distinta.

Manager.dict() resuelve esto lanzando un proceso servidor aparte que es el
único dueño real de los datos; todos los demás procesos hablan con él por
IPC (un socket/pipe interno) cada vez que hacen dict[clave] = valor o leen
dict[clave]. Es más lento que memoria compartida "cruda" (Value/Array),
pero permite guardar estructuras arbitrarias (dicts anidados, strings,
listas) en vez de solo tipos simples de tamaño fijo.
"""


def snapshot_inicial():
    """Estructura base del snapshot. Cada clave es una dimensión, con su timestamp."""
    return {
        "resumen": {"datos": {}, "ts": 0},
        "memoria": {"datos": {}, "ts": 0},
        "sistema": {"datos": {}, "ts": 0},
    }