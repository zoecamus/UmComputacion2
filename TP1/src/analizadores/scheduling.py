import time
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs


def correr(snapshot, intervalo, evento_stop):
    while not evento_stop.is_set():
        datos = {}
        for pid in procfs.listar_pids():
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