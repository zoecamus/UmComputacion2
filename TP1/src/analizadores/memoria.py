import time
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs


def correr(snapshot, intervalo, evento_stop):
    while not evento_stop.is_set():
        datos = {}
        for pid in procfs.listar_pids():
            status = procfs.leer_status(pid)
            if status is None:
                continue
            datos[pid] = {
                "vm_size_kb": status["vm_size_kb"],
                "vm_rss_kb": status["vm_rss_kb"],
                "vm_swap_kb": status["vm_swap_kb"],
            }

        snapshot["memoria"] = {"datos": datos, "ts": time.time()}
        evento_stop.wait(intervalo.value)