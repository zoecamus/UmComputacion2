import time
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs


def correr(snapshot, intervalo, evento_stop):
    """
    Para cada proceso, lista sus threads (TIDs) vía /proc/<pid>/task/.
    Un proceso con 1 thread tiene igual una carpeta task/<pid> (el thread
    principal se ve a sí mismo ahí). Con muchos procesos esto es más caro
    que los otros analizadores porque hay un listdir() + open() extra por
    cada thread de cada proceso -- por eso el intervalo default es más
    largo en la consigna para vistas "pesadas".
    """
    while not evento_stop.is_set():
        datos = {}
        for pid in procfs.listar_pids():
            threads_de_este_proceso = {}
            for tid in procfs.listar_tids(pid):
                info = procfs.leer_stat_thread(pid, tid)
                if info is None:
                    continue  # el thread murió entre el listado y la lectura
                ctx = procfs.leer_ctxt_switches(pid, tid) or {"voluntarios": 0, "involuntarios": 0}
                threads_de_este_proceso[tid] = {
                    "comm": info["comm"],
                    "estado": info["estado"],
                    "ctx_voluntarios": ctx["voluntarios"],
                    "ctx_involuntarios": ctx["involuntarios"],
                }
            if threads_de_este_proceso:
                datos[pid] = threads_de_este_proceso

        snapshot["threads"] = {"datos": datos, "ts": time.time()}
        evento_stop.wait(intervalo.value)