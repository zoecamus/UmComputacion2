import multiprocessing
import signal
import time
import json
import sys

import agregador
from analizadores import resumen, memoria, sistema, threads, senales

# --- Configuración de intervalos (segundos) ---
# Se usan multiprocessing.Value en vez de floats comunes porque estos
# valores los va a leer un PROCESO DISTINTO (el analizador). Un float
# normal de Python no cruza la frontera de fork(); Value sí, porque vive
# en un segmento de memoria compartida real (mmap anónimo por debajo).
INTERVALOS_DEFAULT = {"resumen": 2.0, "memoria": 3.0, "sistema": 2.0, "threads": 2.0, "senales": 10.0}

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
    ]

    for p in procesos:
        p.start()

    print("Monitor corriendo. Ctrl+C para salir, kill -USR1 %d para dump.\n" % __import__("os").getpid())

    try:
        while not evento_stop.is_set():
            time.sleep(1)
            _pintar(snapshot)
    finally:
        evento_stop.set()
        for p in procesos:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()
        print("\nMonitor detenido limpiamente.")


def _pintar(snapshot):
    sis = snapshot["sistema"]["datos"]
    res = snapshot["resumen"]["datos"]
    print("\033c", end="")  # limpia terminal
    if sis:
        print(f"CPU: {sis.get('cpu_pct', 0)}%  "
              f"Load: {sis.get('load1', 0):.2f} {sis.get('load5', 0):.2f} {sis.get('load15', 0):.2f}  "
              f"MemDisp: {sis.get('mem_disponible_kb', 0) // 1024} MB")
    print(f"Procesos activos (vista resumen): {len(res)}")
    print(f"{'PID':>7} {'PPID':>7} {'ESTADO':>7} {'THREADS':>8}  COMANDO")
    for pid, info in list(res.items())[:20]:
        print(f"{pid:>7} {info['ppid']:>7} {info['estado']:>7} {info['threads']:>8}  {info['comm']}")

    thr = snapshot["threads"]["datos"]
    total_threads = sum(len(t) for t in thr.values())
    print(f"\n[threads] {total_threads} threads (LWPs) detectados en {len(thr)} procesos")

    sen = snapshot["senales"]["datos"]
    con_handler = sum(1 for s in sen.values() if s.get("con_handler"))
    print(f"[señales] {con_handler} de {len(sen)} procesos tienen al menos 1 señal con handler propio")


if __name__ == "__main__":
    main()