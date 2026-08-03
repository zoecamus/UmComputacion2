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

    CPU% por thread: mismo principio de delta de jiffies que resumen.py,
    pero la clave del diccionario `anteriores` es (pid, tid) en vez de
    solo pid, porque dos threads del mismo proceso tienen jiffies propios.
    """
    anteriores = {}  # (pid, tid) -> (utime+stime, timestamp)

    while not evento_stop.is_set():
        ahora = time.time()
        datos = {}
        claves_vivas = set()

        for pid in procfs.listar_pids():
            threads_de_este_proceso = {}
            for tid in procfs.listar_tids(pid):
                info = procfs.leer_stat_thread(pid, tid)
                if info is None:
                    continue  # el thread murió entre el listado y la lectura
                ctx = procfs.leer_ctxt_switches(pid, tid) or {"voluntarios": 0, "involuntarios": 0}

                clave = (pid, tid)
                claves_vivas.add(clave)
                jiffies_actuales = info["utime"] + info["stime"]
                cpu_pct = 0.0
                anterior = anteriores.get(clave)
                if anterior is not None:
                    jiffies_prev, ts_prev = anterior
                    delta_seg = ahora - ts_prev
                    if delta_seg > 0:
                        cpu_pct = 100.0 * ((jiffies_actuales - jiffies_prev) / procfs.CLK_TCK) / delta_seg
                anteriores[clave] = (jiffies_actuales, ahora)

                threads_de_este_proceso[tid] = {
                    "comm": info["comm"],
                    "estado": info["estado"],
                    "cpu_pct": round(max(cpu_pct, 0.0), 1),
                    "ctx_voluntarios": ctx["voluntarios"],
                    "ctx_involuntarios": ctx["involuntarios"],
                }
            if threads_de_este_proceso:
                datos[pid] = threads_de_este_proceso

        for clave_vieja in list(anteriores):
            if clave_vieja not in claves_vivas:
                del anteriores[clave_vieja]

        snapshot["threads"] = {"datos": datos, "ts": ahora}
        evento_stop.wait(intervalo.value)