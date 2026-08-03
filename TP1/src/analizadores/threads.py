import time
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs
import ipc


def correr(snapshot, intervalo, evento_stop, cola_pids):
    anteriores = {}  # (pid, tid) -> (utime+stime, timestamp)
    pids_actuales = procfs.listar_pids()  # bootstrap

    while not evento_stop.is_set():
        pids_actuales = ipc.pids_mas_recientes(cola_pids, pids_actuales)
        ahora = time.time()
        datos = {}
        claves_vivas = set()

        for pid in pids_actuales:
            threads_de_este_proceso = {}
            for tid in procfs.listar_tids(pid):
                info = procfs.leer_stat_thread(pid, tid)
                if info is None:
                    continue
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