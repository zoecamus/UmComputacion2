import time
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs
import ipc

HZ = os.sysconf("SC_CLK_TCK")  # clock ticks por segundo del kernel (típicamente 100)


def correr(snapshot, intervalo, evento_stop, cola_pids):
    """
    Ya NO llama a procfs.listar_pids() por su cuenta: recibe la lista de
    PIDs del recolector vía Queue (patrón mailbox, ver ipc.py). El
    bootstrap inicial (antes de que el recolector publique su primera
    lista) usa una lectura directa como fallback, para no arrancar vacío.

    Además de PID/PPID/estado/threads, calcula CPU% por proceso comparando
    (utime+stime) de esta lectura contra la anterior.
    """
    anteriores = {}  # pid -> (utime+stime en jiffies, timestamp de esa lectura)
    pids_actuales = procfs.listar_pids()  # bootstrap, solo la primera vez

    while not evento_stop.is_set():
        pids_actuales = ipc.pids_mas_recientes(cola_pids, pids_actuales)
        ahora = time.time()
        datos = {}
        for pid in pids_actuales:
            info = procfs.leer_stat(pid)
            if info is None:
                continue  # murió entre el listado del recolector y esta lectura
            status = procfs.leer_status(pid)

            cpu_actual = info["utime"] + info["stime"]
            cpu_pct = 0.0
            previo = anteriores.get(pid)
            if previo is not None:
                cpu_prev, t_prev = previo
                delta_t = ahora - t_prev
                if delta_t > 0:
                    cpu_pct = 100.0 * (cpu_actual - cpu_prev) / (HZ * delta_t)
            anteriores[pid] = (cpu_actual, ahora)

            uid = status["uid"] if status else 0
            datos[pid] = {
                "comm": info["comm"],
                "estado": info["estado"],
                "ppid": info["ppid"],
                "threads": status["threads"] if status else 0,
                "cpu_pct": round(max(cpu_pct, 0.0), 1),
                "vm_rss_kb": status["vm_rss_kb"] if status else 0,
                "uid": uid,
                "usuario": procfs.resolver_usuario(uid),
            }

        for pid_viejo in list(anteriores.keys()):
            if pid_viejo not in datos:
                del anteriores[pid_viejo]

        snapshot["resumen"] = {"datos": datos, "ts": time.time()}
        evento_stop.wait(intervalo.value)