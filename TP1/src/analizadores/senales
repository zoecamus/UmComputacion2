import time
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs


def correr(snapshot, intervalo, evento_stop):
    while not evento_stop.is_set():
        datos = {}
        for pid in procfs.listar_pids():
            senales = procfs.leer_senales(pid)
            if senales is None:
                continue
            datos[pid] = senales

        snapshot["senales"] = {"datos": datos, "ts": time.time()}
        evento_stop.wait(intervalo.value)