import time
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs


def correr(colas_pids, intervalo, evento_stop):
    """
    Único componente que llama a procfs.listar_pids(). En vez de que los
    7 analizadores recorran /proc cada uno por su cuenta (7 listdir()
    redundantes por vuelta), el recolector lo hace UNA vez y empuja la
    misma lista a una Queue por analizador.

    Cada Queue tiene maxsize=1 y se usa como "buzón" (mailbox): si el
    analizador todavía no consumió el valor anterior, se descarta y se
    reemplaza por el más nuevo -- a nadie le sirve una lista de PIDs
    vieja, así que no tiene sentido encolar históricos.
    """
    while not evento_stop.is_set():
        pids = procfs.listar_pids()

        for cola in colas_pids.values():
            try:
                cola.get_nowait()  # descarta el valor viejo si no fue consumido
            except Exception:
                pass
            try:
                cola.put_nowait(pids)
            except Exception:
                pass  # si la cola está momentáneamente llena, no bloqueamos al recolector

        evento_stop.wait(intervalo.value)