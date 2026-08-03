import time
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs
import ipc


def correr(snapshot, intervalo, evento_stop, cola_pids):
    pids_actuales = procfs.listar_pids()  # bootstrap

    while not evento_stop.is_set():
        pids_actuales = ipc.pids_mas_recientes(cola_pids, pids_actuales)
        datos = {}
        for pid in pids_actuales:
            senales = procfs.leer_senales(pid)
            if senales is None:
                continue
            datos[pid] = senales

        snapshot["senales"] = {"datos": datos, "ts": time.time()}
        evento_stop.wait(intervalo.value)