import time
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs


def correr(snapshot, intervalo, evento_stop):
    """
    Se ejecuta en un Process propio. Cada `intervalo` segundos, recorre
    los PIDs, arma un resumen y lo escribe en snapshot['resumen'].

    OJO con la escritura: snapshot es un Manager().dict() (un proxy).
    Si hacés snapshot['resumen']['datos'][pid] = x, la mutación interna
    del sub-dict NO se propaga al proceso servidor del Manager, porque
    el proxy solo detecta asignaciones directas sobre sus propias claves.
    Por eso se arma el dict completo en una variable local y recién al
    final se hace snapshot['resumen'] = {...} de una sola vez.
    """
    while not evento_stop.is_set():
        datos = {}
        for pid in procfs.listar_pids():
            info = procfs.leer_stat(pid)
            if info is None:
                continue  # murió entre el listado y la lectura
            status = procfs.leer_status(pid)
            datos[pid] = {
                "comm": info["comm"],
                "estado": info["estado"],
                "ppid": info["ppid"],
                "threads": status["threads"] if status else 0,
            }

        snapshot["resumen"] = {"datos": datos, "ts": time.time()}
        evento_stop.wait(intervalo.value)