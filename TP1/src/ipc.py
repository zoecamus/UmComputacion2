import queue as _queue_mod


def pids_mas_recientes(cola, anterior):
    """
    Patrón 'mailbox': vacía la cola quedándose con el valor más nuevo
    empujado por el recolector. Si no hay nada nuevo desde la última
    vez que se llamó, devuelve la lista anterior -- así el analizador
    NUNCA se bloquea esperando al recolector, simplemente sigue
    trabajando con los PIDs más recientes que tenga a mano.
    """
    ultimo = anterior
    while True:
        try:
            ultimo = cola.get_nowait()
        except _queue_mod.Empty:
            break
    return ultimo