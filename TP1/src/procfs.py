import os

PROC = "/proc"


def listar_pids():
    """PIDs activos: carpetas numéricas de /proc."""
    return [int(n) for n in os.listdir(PROC) if n.isdigit()]


def leer_stat(pid):
    """Parsea /proc/<pid>/stat. None si el proceso ya no existe (TOCTOU)."""
    try:
        with open(f"{PROC}/{pid}/stat") as f:
            linea = f.read()
    except FileNotFoundError:
        return None

    inicio = linea.find('(')
    fin = linea.rfind(')')
    comm = linea[inicio + 1:fin]
    resto = linea[fin + 2:].split()

    # Campos posteriores a comm, 0-indexados desde el campo 3 del stat real:
    # resto[0]=state resto[1]=ppid ... resto[10]=minflt resto[12]=majflt
    # resto[11]=cminflt resto[13]=cmajflt resto[10..16] ver man proc(5)
    return {
        "pid": pid,
        "comm": comm,
        "estado": resto[0],
        "ppid": int(resto[1]),
        "utime": int(resto[11]),   # campo 14 real
        "stime": int(resto[12]),   # campo 15 real
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

    return {
        "threads": num("Threads"),
        "vm_size_kb": num("VmSize"),
        "vm_rss_kb": num("VmRSS"),
        "vm_swap_kb": num("VmSwap"),
    }


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
    return {"tid": tid, "comm": comm, "estado": resto[0]}


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