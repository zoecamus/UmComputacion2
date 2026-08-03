import multiprocessing
import signal
import time
import json
import sys
import curses
import os

import agregador
import display
import recolector
from analizadores import resumen, memoria, sistema, threads, senales, fds, scheduling

RUTA_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config.json")

# Fallback si config.json no existe o está corrupto -- el programa tiene
# que poder arrancar igual, no depender ciegamente de un archivo externo.
INTERVALOS_DEFAULT = {"recolector": 1.0, "resumen": 2.0, "memoria": 3.0, "sistema": 2.0, "threads": 2.0,
                       "senales": 10.0, "fds": 5.0, "scheduling": 10.0}

MINIMOS = {v[3]: v[4] for v in display.VISTAS}  # clave -> intervalo mínimo permitido


def cargar_config():
    try:
        with open(RUTA_CONFIG) as f:
            data = json.load(f)
        intervalos_cfg = data.get("intervalos", {})
        resultado = dict(INTERVALOS_DEFAULT)
        for clave, valor in intervalos_cfg.items():
            if clave in resultado:
                resultado[clave] = max(float(valor), MINIMOS.get(clave, 0.1))
        return resultado
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return dict(INTERVALOS_DEFAULT)

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


def hacer_manejador_reload(intervalos):
    """
    SIGHUP: releer config.json y actualizar los multiprocessing.Value de
    intervalo EN CALIENTE, sin reiniciar ningún proceso. Los analizadores
    ya están leyendo `intervalo.value` en cada vuelta de su loop (nunca
    cachean el número), así que el cambio se aplica solo en la próxima
    iteración de cada uno -- no hace falta avisarles de ninguna otra forma.
    """
    def manejar_sighup(signum, frame):
        nuevos = cargar_config()
        for clave, valor in nuevos.items():
            if clave in intervalos:
                with intervalos[clave].get_lock():
                    intervalos[clave].value = valor
        print(f"\n[reload] config.json releído: {nuevos}", file=sys.stderr)
    return manejar_sighup


def hacer_manejador_verbose(modo_verbose):
    """SIGUSR2: alterna un flag de modo verbose, compartido vía Value con la TUI."""
    def manejar_sigusr2(signum, frame):
        with modo_verbose.get_lock():
            modo_verbose.value = 0 if modo_verbose.value else 1
        print(f"\n[verbose] modo verbose = {bool(modo_verbose.value)}", file=sys.stderr)
    return manejar_sigusr2


def main():
    manager = multiprocessing.Manager()
    snapshot = manager.dict(agregador.snapshot_inicial())

    intervalos_iniciales = cargar_config()
    intervalos = {
        nombre: multiprocessing.Value("d", seg)
        for nombre, seg in intervalos_iniciales.items()
    }
    modo_verbose = multiprocessing.Value("i", 0)

    # Una Queue(maxsize=1) por analizador -- el recolector empuja la misma
    # lista de PIDs a las 7, usadas como "buzón" (ver ipc.pids_mas_recientes).
    NOMBRES_ANALIZADORES = ["resumen", "memoria", "sistema", "threads", "senales", "fds", "scheduling"]
    colas_pids = {nombre: multiprocessing.Queue(maxsize=1) for nombre in NOMBRES_ANALIZADORES}

    signal.signal(signal.SIGINT, manejar_sigint_sigterm)
    signal.signal(signal.SIGTERM, manejar_sigint_sigterm)
    signal.signal(signal.SIGUSR1, hacer_manejador_dump(snapshot))
    signal.signal(signal.SIGHUP, hacer_manejador_reload(intervalos))
    signal.signal(signal.SIGUSR2, hacer_manejador_verbose(modo_verbose))

    procesos = [
        multiprocessing.Process(
            target=recolector.correr,
            args=(colas_pids, intervalos["recolector"], evento_stop),
            name="recolector",
        ),
        multiprocessing.Process(
            target=resumen.correr,
            args=(snapshot, intervalos["resumen"], evento_stop, colas_pids["resumen"]),
            name="analizador-resumen",
        ),
        multiprocessing.Process(
            target=memoria.correr,
            args=(snapshot, intervalos["memoria"], evento_stop, colas_pids["memoria"]),
            name="analizador-memoria",
        ),
        multiprocessing.Process(
            target=sistema.correr,
            args=(snapshot, intervalos["sistema"], evento_stop, colas_pids["sistema"]),
            name="analizador-sistema",
        ),
        multiprocessing.Process(
            target=threads.correr,
            args=(snapshot, intervalos["threads"], evento_stop, colas_pids["threads"]),
            name="analizador-threads",
        ),
        multiprocessing.Process(
            target=senales.correr,
            args=(snapshot, intervalos["senales"], evento_stop, colas_pids["senales"]),
            name="analizador-senales",
        ),
        multiprocessing.Process(
            target=fds.correr,
            args=(snapshot, intervalos["fds"], evento_stop, colas_pids["fds"]),
            name="analizador-fds",
        ),
        multiprocessing.Process(
            target=scheduling.correr,
            args=(snapshot, intervalos["scheduling"], evento_stop, colas_pids["scheduling"]),
            name="analizador-scheduling",
        ),
    ]

    for p in procesos:
        p.start()

    try:
        curses.wrapper(display.run, snapshot, intervalos, evento_stop, modo_verbose)
    finally:
        evento_stop.set()
        for p in procesos:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()
        print("Monitor detenido limpiamente.")


if __name__ == "__main__":
    main()