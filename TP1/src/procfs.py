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