import os

PROC = "/proc"


def listar_pids():
    """PIDs activos: carpetas numéricas de /proc."""
    return [int(n) for n in os.listdir(PROC) if n.isdigit()]


_POLICIAS = {0: "SCHED_OTHER", 1: "SCHED_FIFO", 2: "SCHED_RR",
             3: "SCHED_BATCH", 5: "SCHED_IDLE", 6: "SCHED_DEADLINE"}

CLK_TCK = os.sysconf("SC_CLK_TCK")  # jiffies por segundo, típicamente 100


def leer_stat(pid):
    """
    Parsea /proc/<pid>/stat. None si el proceso ya no existe (TOCTOU).

    Formato real (man proc(5)): "<pid> (<comm>) <resto de campos>".
    `comm` va entre el PRIMER '(' y el ÚLTIMO ')' porque puede contener
    espacios o paréntesis. Todo lo posterior son campos separados por un
    solo espacio, numerados desde el campo 3 (el propio 'comm' es el 2).
    Por eso resto[0] es el campo 3, resto[i] es el campo (i+3).
    """
    try:
        with open(f"{PROC}/{pid}/stat") as f:
            linea = f.read()
    except FileNotFoundError:
        return None

    inicio = linea.find('(')
    fin = linea.rfind(')')
    comm = linea[inicio + 1:fin]
    resto = linea[fin + 2:].split()

    policy_num = int(resto[38]) if len(resto) > 38 else 0

    return {
        "pid": pid,
        "comm": comm,
        "estado": resto[0],                    # campo 3
        "ppid": int(resto[1]),                  # campo 4
        "pgrp": int(resto[2]),                  # campo 5 (PGID)
        "sid": int(resto[3]),                   # campo 6 (SID)
        "minflt": int(resto[7]),                # campo 10
        "majflt": int(resto[9]),                # campo 12
        "utime": int(resto[11]),                # campo 14
        "stime": int(resto[12]),                # campo 15
        "priority": int(resto[15]),              # campo 18
        "nice": int(resto[16]),                  # campo 19
        "num_threads": int(resto[17]),           # campo 20
        "rt_priority": int(resto[37]) if len(resto) > 37 else 0,  # campo 40
        "policy_num": policy_num,                                  # campo 41
        "policy": _POLICIAS.get(policy_num, f"SCHED_{policy_num}"),
    }


def leer_cmdline(pid):
    """Lista de argumentos de /proc/<pid>/cmdline (separado por \\0)."""
    try:
        with open(f"{PROC}/{pid}/cmdline", "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return []
    if not data:
        return []
    return data.decode(errors="replace").split('\0')[:-1]


def leer_status(pid):
    """Parsea /proc/<pid>/status (formato clave: valor). None si no existe."""
    campos = {}
    try:
        with open(f"{PROC}/{pid}/status") as f:
            for linea in f:
                if ":" not in linea:
                    continue
                clave, valor = linea.split(":", 1)
                campos[clave.strip()] = valor.strip()
    except FileNotFoundError:
        return None

    def num(clave, default=0):
        v = campos.get(clave, "")
        v = v.split()[0] if v else ""
        return int(v) if v.isdigit() else default

    uid_real = campos.get("Uid", "0").split()[0]

    return {
        "threads": num("Threads"),
        "vm_size_kb": num("VmSize"),
        "vm_rss_kb": num("VmRSS"),
        "vm_hwm_kb": num("VmHWM"),
        "vm_data_kb": num("VmData"),
        "vm_stk_kb": num("VmStk"),
        "vm_exe_kb": num("VmExe"),
        "vm_lib_kb": num("VmLib"),
        "vm_swap_kb": num("VmSwap"),
        "uid": int(uid_real) if uid_real.isdigit() else 0,
        "ctxt_voluntarios": num("voluntary_ctxt_switches"),
        "ctxt_involuntarios": num("nonvoluntary_ctxt_switches"),
        "cpus_allowed": campos.get("Cpus_allowed_list", ""),
    }


_CACHE_USUARIOS = {}


def resolver_usuario(uid):
    """uid numérico -> nombre de usuario, usando /etc/passwd. Cachea resultados."""
    if uid in _CACHE_USUARIOS:
        return _CACHE_USUARIOS[uid]
    try:
        import pwd
        nombre = pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        nombre = str(uid)
    _CACHE_USUARIOS[uid] = nombre
    return nombre


def leer_meminfo():
    """Parsea /proc/meminfo -> dict con valores en kB."""
    datos = {}
    with open(f"{PROC}/meminfo") as f:
        for linea in f:
            clave, resto = linea.split(":", 1)
            valor = resto.strip().split()[0]
            datos[clave] = int(valor)
    return datos


def leer_loadavg():
    """Parsea /proc/loadavg -> (load1, load5, load15)."""
    with open(f"{PROC}/loadavg") as f:
        partes = f.read().split()
    return float(partes[0]), float(partes[1]), float(partes[2])


def leer_cpu_global():
    """Primera línea 'cpu' de /proc/stat -> dict con jiffies user/nice/system/idle/iowait."""
    with open(f"{PROC}/stat") as f:
        primera = f.readline().split()
    etiquetas = ["user", "nice", "system", "idle", "iowait", "irq", "softirq"]
    valores = [int(x) for x in primera[1:]]
    return dict(zip(etiquetas, valores))


def leer_uptime_boot():
    """/proc/uptime (segundos desde el boot) y btime de /proc/stat (epoch del boot)."""
    with open(f"{PROC}/uptime") as f:
        uptime_seg = float(f.read().split()[0])
    btime = 0
    with open(f"{PROC}/stat") as f:
        for linea in f:
            if linea.startswith("btime"):
                btime = int(linea.split()[1])
                break
    return uptime_seg, btime


def leer_maps_resumen(pid):
    """
    Agrupa /proc/<pid>/maps (líneas 'start-end perms offset dev inode path')
    en categorías por tamaño total en KB: heap, stack, texto (código, perms
    con 'x'), datos (perms 'rw-' sin ser heap/stack), y resto (bibliotecas
    compartidas, mappings anónimos, etc). None si no existe o sin permiso.
    """
    try:
        with open(f"{PROC}/{pid}/maps") as f:
            lineas = f.readlines()
    except (FileNotFoundError, PermissionError):
        return None

    grupos = {"heap": 0, "stack": 0, "texto": 0, "datos": 0, "resto": 0}
    for linea in lineas:
        partes = linea.split(None, 5)
        rango = partes[0]
        perms = partes[1]
        pathname = partes[5].strip() if len(partes) > 5 else ""

        inicio_s, fin_s = rango.split("-")
        tam_kb = (int(fin_s, 16) - int(inicio_s, 16)) // 1024

        if pathname == "[heap]":
            grupos["heap"] += tam_kb
        elif pathname.startswith("[stack"):
            grupos["stack"] += tam_kb
        elif "x" in perms:
            grupos["texto"] += tam_kb
        elif "w" in perms and not pathname.startswith("/"):
            grupos["datos"] += tam_kb
        else:
            grupos["resto"] += tam_kb

    return grupos


# ---------- THREADS (LWPs) ----------

def listar_tids(pid):
    """TIDs (thread IDs) de un proceso: carpetas numéricas de /proc/<pid>/task."""
    try:
        return [int(n) for n in os.listdir(f"{PROC}/{pid}/task") if n.isdigit()]
    except FileNotFoundError:
        return []


def leer_stat_thread(pid, tid):
    """Igual que leer_stat pero para un thread individual (mismo formato de archivo)."""
    try:
        with open(f"{PROC}/{pid}/task/{tid}/stat") as f:
            linea = f.read()
    except FileNotFoundError:
        return None
    inicio = linea.find('(')
    fin = linea.rfind(')')
    comm = linea[inicio + 1:fin]
    resto = linea[fin + 2:].split()
    return {
        "tid": tid,
        "comm": comm,
        "estado": resto[0],
        "utime": int(resto[11]),
        "stime": int(resto[12]),
    }


def leer_ctxt_switches(pid, tid):
    """voluntary_ctxt_switches / nonvoluntary_ctxt_switches desde status del thread."""
    try:
        with open(f"{PROC}/{pid}/task/{tid}/status") as f:
            contenido = f.read()
    except FileNotFoundError:
        return None
    vol = nonvol = 0
    for linea in contenido.splitlines():
        if linea.startswith("voluntary_ctxt_switches:"):
            vol = int(linea.split(":")[1].strip())
        elif linea.startswith("nonvoluntary_ctxt_switches:"):
            nonvol = int(linea.split(":")[1].strip())
    return {"voluntarios": vol, "involuntarios": nonvol}


# ---------- SEÑALES ----------

import signal as _signal_mod

_NOMBRES_SENAL = {}
for _i in range(1, 65):
    try:
        _NOMBRES_SENAL[_i] = _signal_mod.Signals(_i).name
    except ValueError:
        _NOMBRES_SENAL[_i] = f"SIG{_i}"  # número real-time sin nombre estándar


def decodificar_mascara(hexstr):
    """
    Convierte una máscara hex de 64 bits (ej: SigBlk) en la lista de nombres
    de señal cuyo bit está en 1. El bit N-1 corresponde a la señal N
    (bit 0 = SIGHUP=1, bit 1 = SIGINT=2, etc. — la señal 0 no existe).
    """
    valor = int(hexstr, 16)
    activas = []
    for numero_senal in range(1, 65):
        if valor & (1 << (numero_senal - 1)):
            activas.append(_NOMBRES_SENAL.get(numero_senal, f"SIG{numero_senal}"))
    return activas


def leer_senales(pid):
    """Lee SigBlk/SigIgn/SigCgt/SigPnd/ShdPnd de /proc/<pid>/status y los decodifica."""
    campos = {}
    try:
        with open(f"{PROC}/{pid}/status") as f:
            for linea in f:
                if ":" not in linea:
                    continue
                clave, valor = linea.split(":", 1)
                campos[clave.strip()] = valor.strip()
    except FileNotFoundError:
        return None

    claves = {"SigBlk": "bloqueadas", "SigIgn": "ignoradas", "SigCgt": "con_handler",
              "SigPnd": "pendientes", "ShdPnd": "pendientes_grupo"}
    resultado = {}
    for clave_proc, nombre in claves.items():
        hexstr = campos.get(clave_proc, "0")
        resultado[nombre] = decodificar_mascara(hexstr)
    return resultado


# ---------- FILE DESCRIPTORS ----------

def leer_fds(pid):
    """
    Lista /proc/<pid>/fd/: cada entrada es un symlink a lo que apunta ese FD
    (un archivo real, un socket, una pipe, etc). Requiere permisos -- si el
    proceso no es tuyo (ni corrés como root), esto puede dar PermissionError,
    que se trata igual que "no pude leerlo" y no como un crash.

    Devuelve total, conteo_por_tipo, y la LISTA detallada (fd, tipo, destino)
    que pide la consigna para la vista de File Descriptors.
    """
    ruta = f"{PROC}/{pid}/fd"
    try:
        entradas = os.listdir(ruta)
    except (FileNotFoundError, PermissionError):
        return None

    conteo = {"archivo": 0, "socket": 0, "pipe": 0, "anon_inode": 0, "otro": 0}
    detalle = []
    for fd in sorted(entradas, key=lambda x: int(x)):
        try:
            destino = os.readlink(f"{ruta}/{fd}")
        except (FileNotFoundError, PermissionError):
            continue  # el FD se cerró entre el listado y la lectura (TOCTOU otra vez)

        if destino.startswith("socket:"):
            tipo = "socket"
        elif destino.startswith("pipe:"):
            tipo = "pipe"
        elif destino.startswith("anon_inode:"):
            tipo = "anon_inode"
        elif destino.startswith("/dev/pts") or destino.startswith("/dev/tty"):
            tipo = "tty"
        elif destino.startswith("/"):
            tipo = "archivo"
        else:
            tipo = "otro"

        conteo[tipo] = conteo.get(tipo, 0) + 1
        detalle.append({"fd": int(fd), "tipo": tipo, "destino": destino})

    return {"total": len(entradas), "por_tipo": conteo, "detalle": detalle}