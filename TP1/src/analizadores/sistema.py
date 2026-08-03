import time
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs


def correr(snapshot, intervalo, evento_stop):
    """
    CPU% global se calcula por DELTA de jiffies entre dos lecturas
    consecutivas de /proc/stat, no de una lectura sola (una sola lectura
    solo te da un acumulado desde el boot, no un porcentaje "ahora").
    """
    anterior = procfs.leer_cpu_global()

    while not evento_stop.is_set():
        evento_stop.wait(intervalo.value)
        if evento_stop.is_set():
            break

        actual = procfs.leer_cpu_global()
        delta_total = sum(actual.values()) - sum(anterior.values())
        delta_idle = actual["idle"] - anterior["idle"]
        cpu_pct = 0.0
        if delta_total > 0:
            cpu_pct = 100.0 * (delta_total - delta_idle) / delta_total
        anterior = actual

        meminfo = procfs.leer_meminfo()
        load1, load5, load15 = procfs.leer_loadavg()
        uptime_seg, btime = procfs.leer_uptime_boot()

        # Conteo de procesos por estado y threads totales: recorremos
        # /proc de nuevo acá (independiente del analizador resumen, que
        # corre en su propio proceso con su propio intervalo).
        por_estado = {}
        threads_totales = 0
        zombies = 0
        total_procesos = 0
        for pid in procfs.listar_pids():
            info = procfs.leer_stat(pid)
            if info is None:
                continue
            total_procesos += 1
            por_estado[info["estado"]] = por_estado.get(info["estado"], 0) + 1
            threads_totales += info["num_threads"]
            if info["estado"] == "Z":
                zombies += 1

        datos = {
            "cpu_pct": round(cpu_pct, 1),
            "mem_total_kb": meminfo.get("MemTotal", 0),
            "mem_libre_kb": meminfo.get("MemFree", 0),
            "mem_disponible_kb": meminfo.get("MemAvailable", 0),
            "mem_buffers_kb": meminfo.get("Buffers", 0),
            "mem_cached_kb": meminfo.get("Cached", 0),
            "mem_swap_total_kb": meminfo.get("SwapTotal", 0),
            "mem_swap_libre_kb": meminfo.get("SwapFree", 0),
            "load1": load1,
            "load5": load5,
            "load15": load15,
            "uptime_seg": uptime_seg,
            "boot_epoch": btime,
            "total_procesos": total_procesos,
            "por_estado": por_estado,
            "threads_totales": threads_totales,
            "zombies": zombies,
        }

        snapshot["sistema"] = {"datos": datos, "ts": time.time()}