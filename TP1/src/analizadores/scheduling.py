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
            info = procfs.leer_stat(pid)
            if info is None:
                continue
            status = procfs.leer_status(pid)
            datos[pid] = {
                "comm": info["comm"],
                "nice": info["nice"],
                "priority": info["priority"],
                "rt_priority": info["rt_priority"],
                "policy": info["policy"],
                "pgrp": info["pgrp"],
                "sid": info["sid"],
                "cpus_allowed": status["cpus_allowed"] if status else "",
                "ctxt_voluntarios": status["ctxt_voluntarios"] if status else 0,
                "ctxt_involuntarios": status["ctxt_involuntarios"] if status else 0,
                "utime": info["utime"],
                "stime": info["stime"],
            }

        snapshot["scheduling"] = {"datos": datos, "ts": time.time()}
        evento_stop.wait(intervalo.value)