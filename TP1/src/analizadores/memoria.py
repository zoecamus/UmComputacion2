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
            stat = procfs.leer_stat(pid)  # para page faults (campos 10 y 12)
            segmentos = procfs.leer_maps_resumen(pid)  # puede ser None sin permiso

            datos[pid] = {
                "vm_size_kb": status["vm_size_kb"],
                "vm_rss_kb": status["vm_rss_kb"],
                "vm_hwm_kb": status["vm_hwm_kb"],
                "vm_data_kb": status["vm_data_kb"],
                "vm_stk_kb": status["vm_stk_kb"],
                "vm_exe_kb": status["vm_exe_kb"],
                "vm_lib_kb": status["vm_lib_kb"],
                "vm_swap_kb": status["vm_swap_kb"],
                "minflt": stat["minflt"] if stat else 0,
                "majflt": stat["majflt"] if stat else 0,
                "segmentos": segmentos,  # dict heap/stack/texto/datos/resto en KB, o None
            }

        snapshot["memoria"] = {"datos": datos, "ts": time.time()}
        evento_stop.wait(intervalo.value)