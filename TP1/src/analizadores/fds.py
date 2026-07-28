import time
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import procfs


def correr(snapshot, intervalo, evento_stop):
    """
    Cuenta los FDs abiertos por proceso y los clasifica por tipo
    (archivo/socket/pipe/anon_inode) según a qué apunta el symlink en
    /proc/<pid>/fd/<n>. Procesos que no son nuestros dan PermissionError
    (procfs.leer_fds ya lo maneja devolviendo None), así que simplemente
    los salteamos -- es esperable, no un bug.
    """
    while not evento_stop.is_set():
        datos = {}
        for pid in procfs.listar_pids():
            info = procfs.leer_fds(pid)
            if info is None:
                continue
            datos[pid] = info

        snapshot["fds"] = {"datos": datos, "ts": time.time()}
        evento_stop.wait(intervalo.value)