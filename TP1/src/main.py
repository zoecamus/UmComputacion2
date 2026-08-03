import multiprocessing
import signal
import time
import json
import sys
import curses
import os

import agregador
import display
from analizadores import resumen, memoria, sistema, threads, senales, fds, scheduling

# --- Configuración de intervalos (segundos) ---
# Se usan multiprocessing.Value en vez de floats comunes porque estos
# valores los va a leer un PROCESO DISTINTO (el analizador). Un float
# normal de Python no cruza la frontera de fork(); Value sí, porque vive
# en un segmento de memoria compartida real (mmap anónimo por debajo).
INTERVALOS_DEFAULT = {"resumen": 2.0, "memoria": 3.0, "sistema": 2.0, "threads": 2.0,
                       "senales": 10.0, "fds": 5.0, "scheduling": 10.0}

evento_stop = multiprocessing.Event()


def manejar_sigint_sigterm(signum, frame):
    # Handler simple: solo marca la bandera. Async-signal-safe de verdad
    # implicaría no tocar más que esto (nada de I/O acá). El trabajo pesado
    # (join de procesos, flush) se hace afuera, en el loop principal.
    evento_stop.set()


def hacer_manejador_dump(snapshot):
    def manejar_sigusr1(signum, frame):
        ts = int(time.time())
        nombre = f"dump_{ts}.json"
        # snapshot es un proxy de Manager; lo convertimos a dict plano para json.dump
        plano = {k: dict(v) for k, v in snapshot.items()}
        with open(nombre, "w") as f:
            json.dump(plano, f, indent=2, default=str)
        print(f"\n[dump] snapshot guardado en {nombre}", file=sys.stderr)
    return manejar_sigusr1


def main():
    manager = multiprocessing.Manager()
    snapshot = manager.dict(agregador.snapshot_inicial())

    intervalos = {
        nombre: multiprocessing.Value("d", seg)
        for nombre, seg in INTERVALOS_DEFAULT.items()
    }

    signal.signal(signal.SIGINT, manejar_sigint_sigterm)
    signal.signal(signal.SIGTERM, manejar_sigint_sigterm)
    signal.signal(signal.SIGUSR1, hacer_manejador_dump(snapshot))

    procesos = [
        multiprocessing.Process(
            target=resumen.correr,
            args=(snapshot, intervalos["resumen"], evento_stop),
            name="analizador-resumen",
        ),
        multiprocessing.Process(
            target=memoria.correr,
            args=(snapshot, intervalos["memoria"], evento_stop),
            name="analizador-memoria",
        ),
        multiprocessing.Process(
            target=sistema.correr,
            args=(snapshot, intervalos["sistema"], evento_stop),
            name="analizador-sistema",
        ),
        multiprocessing.Process(
            target=threads.correr,
            args=(snapshot, intervalos["threads"], evento_stop),
            name="analizador-threads",
        ),
        multiprocessing.Process(
            target=senales.correr,
            args=(snapshot, intervalos["senales"], evento_stop),
            name="analizador-senales",
        ),
        multiprocessing.Process(
            target=fds.correr,
            args=(snapshot, intervalos["fds"], evento_stop),
            name="analizador-fds",
        ),
        multiprocessing.Process(
            target=scheduling.correr,
            args=(snapshot, intervalos["scheduling"], evento_stop),
            name="analizador-scheduling",
        ),
    ]

    for p in procesos:
        p.start()

    try:
        curses.wrapper(display.run, snapshot, intervalos, evento_stop)
    finally:
        evento_stop.set()
        for p in procesos:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()
        print("Monitor detenido limpiamente.")


if __name__ == "__main__":
    main()