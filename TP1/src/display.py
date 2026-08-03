"""
Display (TUI) del monitor. Corre en el proceso PRINCIPAL (main.py), no en
un Process propio -- es el único componente que necesita terminal
interactiva, y la consigna permite usar threads acá adentro si hiciera
falta (no los necesitamos: curses ya maneja teclado de forma no bloqueante
con stdscr.timeout()).

Este módulo SOLO LEE el snapshot compartido (nunca escribe en él, salvo
los multiprocessing.Value de intervalo cuando el usuario aprieta +/-).
No conoce nada de multiprocessing.Process ni de cómo se generan los datos:
esa separación es a propósito, mismo principio que separar procfs.py
(lectura pura) de los analizadores (orquestación).
"""
import curses

# (número, tecla_letra, título, clave_snapshot, intervalo_mínimo)
VISTAS = [
    (1, "r", "Resumen", "resumen", 0.5),
    (2, "m", "Memoria", "memoria", 1.0),
    (3, "f", "FDs", "fds", 2.0),
    (4, "t", "Threads", "threads", 0.5),
    (5, "s", "Señales", "senales", 5.0),
    (6, "p", "Scheduling", "scheduling", 5.0),
    (7, "g", "Sistema", "sistema", 1.0),
]

ORDEN_CICLO = ["pid", "cpu", "rss"]


def _fmt_kb(kb):
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.1f}G"
    if kb >= 1024:
        return f"{kb / 1024:.1f}M"
    return f"{kb}K"


def _lista_ordenada(datos_resumen, orden, filtro_comm, filtro_user, pid_pineado):
    """
    datos_resumen: dict {pid: {comm, estado, ppid, threads, cpu_pct,
    vm_rss_kb, uid, usuario}}. Devuelve lista de (pid, info) filtrada y
    ordenada, con el pid pineado (si sigue existiendo y pasa el filtro)
    siempre primero.
    """
    items = list(datos_resumen.items())

    if filtro_comm:
        items = [(p, i) for p, i in items if filtro_comm.lower() in i["comm"].lower()]
    if filtro_user:
        items = [(p, i) for p, i in items if filtro_user.lower() in i["usuario"].lower()]

    if orden == "cpu":
        items.sort(key=lambda pi: pi[1]["cpu_pct"], reverse=True)
    elif orden == "rss":
        items.sort(key=lambda pi: pi[1]["vm_rss_kb"], reverse=True)
    else:
        items.sort(key=lambda pi: pi[0])

    if pid_pineado is not None:
        pineado = [(p, i) for p, i in items if p == pid_pineado]
        resto = [(p, i) for p, i in items if p != pid_pineado]
        items = pineado + resto

    return items


def _prompt(stdscr, etiqueta):
    """Lee una línea de texto en la última fila de la pantalla. Devuelve el string (puede ser vacío)."""
    altura, ancho = stdscr.getmaxyx()
    fila = altura - 1
    stdscr.move(fila, 0)
    stdscr.clrtoeol()
    stdscr.addstr(fila, 0, etiqueta)
    curses.echo()
    curses.curs_set(1)
    stdscr.timeout(-1)  # bloqueante mientras se escribe, así no se corta el input
    try:
        texto = stdscr.getstr(fila, len(etiqueta), 60).decode("utf-8", errors="replace")
    except curses.error:
        texto = ""
    curses.noecho()
    curses.curs_set(0)
    stdscr.timeout(300)
    return texto.strip()


def _dibujar_ayuda(stdscr):
    lineas = [
        "AYUDA — teclas",
        "",
        "1-7 o r/m/f/t/s/p/g   cambiar de vista",
        "flechas arriba/abajo   navegar la lista de procesos",
        "Enter                  pin/unpin del proceso seleccionado",
        "/                      filtrar por nombre de comando",
        "u                      filtrar por usuario",
        "c                      ciclar orden: PID / CPU% / RSS",
        "+ / -                  ajustar intervalo de la vista activa",
        "q                      salir",
        "h / ?                  esta ayuda (cualquier tecla para cerrar)",
    ]
    altura, ancho = stdscr.getmaxyx()
    h = len(lineas) + 2
    w = max(len(l) for l in lineas) + 4
    y0 = max(0, (altura - h) // 2)
    x0 = max(0, (ancho - w) // 2)
    win = curses.newwin(h, w, y0, x0)
    win.box()
    for i, linea_txt in enumerate(lineas):
        try:
            win.addstr(i + 1, 2, linea_txt)
        except curses.error:
            pass
    win.refresh()
    win.timeout(-1)
    win.getch()


def run(stdscr, snapshot, intervalos, evento_stop):
    curses.curs_set(0)
    stdscr.timeout(300)  # no bloqueante: refresca solo aunque no haya tecla

    vista_idx = 0
    cursor_pid = None
    pid_pineado = None
    orden = "pid"
    filtro_comm = ""
    filtro_user = ""

    while not evento_stop.is_set():
        try:
            tecla = stdscr.getch()
        except curses.error:
            tecla = -1

        numero, letra, titulo, clave, intervalo_min = VISTAS[vista_idx]

        # --- Manejo de teclas ---
        if tecla in (ord("q"), ord("Q")):
            evento_stop.set()
            break
        elif tecla in (ord("h"), ord("?")):
            _dibujar_ayuda(stdscr)
        elif ord("1") <= tecla <= ord("7"):
            vista_idx = tecla - ord("1")
        elif tecla in (ord(v[1]) for v in VISTAS):
            for i, v in enumerate(VISTAS):
                if tecla == ord(v[1]):
                    vista_idx = i
                    break
        elif tecla == ord("/"):
            filtro_comm = _prompt(stdscr, "Filtrar por comando: ")
        elif tecla == ord("u"):
            filtro_user = _prompt(stdscr, "Filtrar por usuario: ")
        elif tecla == ord("c"):
            orden = ORDEN_CICLO[(ORDEN_CICLO.index(orden) + 1) % len(ORDEN_CICLO)]
        elif tecla in (ord("+"), ord("=")):
            with intervalos[clave].get_lock():
                intervalos[clave].value = round(intervalos[clave].value + 0.5, 1)
        elif tecla == ord("-"):
            with intervalos[clave].get_lock():
                nuevo = round(intervalos[clave].value - 0.5, 1)
                intervalos[clave].value = max(nuevo, intervalo_min)

        # --- Datos actuales ---
        datos_resumen = snapshot["resumen"]["datos"]
        lista = _lista_ordenada(datos_resumen, orden, filtro_comm, filtro_user, pid_pineado)
        pids_visibles = [p for p, _ in lista]

        if tecla == curses.KEY_UP:
            if cursor_pid in pids_visibles:
                idx = pids_visibles.index(cursor_pid)
                cursor_pid = pids_visibles[max(0, idx - 1)]
            elif pids_visibles:
                cursor_pid = pids_visibles[0]
        elif tecla == curses.KEY_DOWN:
            if cursor_pid in pids_visibles:
                idx = pids_visibles.index(cursor_pid)
                cursor_pid = pids_visibles[min(len(pids_visibles) - 1, idx + 1)]
            elif pids_visibles:
                cursor_pid = pids_visibles[0]
        elif tecla in (curses.KEY_ENTER, 10, 13):
            if cursor_pid is not None:
                pid_pineado = None if pid_pineado == cursor_pid else cursor_pid

        if cursor_pid not in pids_visibles and pids_visibles:
            cursor_pid = pids_visibles[0]

        _dibujar(stdscr, snapshot, intervalos, vista_idx, lista, cursor_pid,
                 pid_pineado, orden, filtro_comm, filtro_user)

    return


def _dibujar(stdscr, snapshot, intervalos, vista_idx, lista, cursor_pid,
              pid_pineado, orden, filtro_comm, filtro_user):
    stdscr.erase()
    altura, ancho = stdscr.getmaxyx()
    numero, letra, titulo, clave, intervalo_min = VISTAS[vista_idx]

    # --- Barra de pestañas ---
    partes_tabs = []
    for n, l, t, c, _ in VISTAS:
        marca = f"[{n}:{t}]" if (n - 1) == vista_idx else f" {n}:{t} "
        partes_tabs.append(marca)
    linea_tabs = " ".join(partes_tabs)
    try:
        stdscr.addstr(0, 0, linea_tabs[:ancho - 1], curses.A_REVERSE)
    except curses.error:
        pass

    info_extra = f"orden={orden} intervalo={intervalos[clave].value:.1f}s(min {intervalo_min})"
    if filtro_comm:
        info_extra += f" filtro_cmd='{filtro_comm}'"
    if filtro_user:
        info_extra += f" filtro_user='{filtro_user}'"
    try:
        stdscr.addstr(1, 0, info_extra[:ancho - 1])
    except curses.error:
        pass

    # --- Lista de procesos (siempre visible, arriba) ---
    fila = 3
    encabezado = f"{'':2}{'PID':>7} {'PPID':>7} {'USR':>8} {'ST':>3} {'CPU%':>6} {'RSS':>8} {'THR':>4}  COMANDO"
    try:
        stdscr.addstr(fila, 0, encabezado[:ancho - 1], curses.A_BOLD)
    except curses.error:
        pass
    fila += 1

    alto_lista = max(3, (altura - fila) // 2)
    for pid, info in lista[:alto_lista]:
        marca_pin = "*" if pid == pid_pineado else " "
        linea = (f"{marca_pin}{pid:>7} {info['ppid']:>7} {info['usuario']:>8.8} "
                 f"{info['estado']:>3} {info['cpu_pct']:>6.1f} {_fmt_kb(info['vm_rss_kb']):>8} "
                 f"{info['threads']:>4}  {info['comm']}")
        attr = curses.A_REVERSE if pid == cursor_pid else curses.A_NORMAL
        try:
            stdscr.addstr(fila, 0, linea[:ancho - 1], attr)
        except curses.error:
            pass
        fila += 1
        if fila >= altura - 1:
            break

    # --- Panel de detalle (según vista activa) ---
    fila += 1
    if fila < altura - 1:
        try:
            stdscr.addstr(fila, 0, f"--- Detalle: {titulo} ---"[:ancho - 1], curses.A_BOLD)
        except curses.error:
            pass
        fila += 1
        _dibujar_detalle(stdscr, snapshot, clave, cursor_pid, fila, altura, ancho)

    try:
        stdscr.addstr(altura - 1, 0,
                       "q:salir h:ayuda 1-7:vista flechas:navegar Enter:pin /:filtro u:usuario c:orden +/-:intervalo"[:ancho - 1])
    except curses.error:
        pass

    stdscr.refresh()


def _dibujar_detalle(stdscr, snapshot, clave, pid, fila, altura, ancho):
    def linea(texto):
        nonlocal fila
        if fila < altura - 1:
            try:
                stdscr.addstr(fila, 0, texto[:ancho - 1])
            except curses.error:
                pass
            fila += 1

    if pid is None:
        linea("(sin proceso seleccionado)")
        return

    if clave == "resumen":
        info = snapshot["resumen"]["datos"].get(pid)
        if not info:
            linea("(sin datos)")
            return
        linea(f"PID {pid}  comm={info['comm']}  estado={info['estado']}  ppid={info['ppid']}")
        linea(f"usuario={info['usuario']} (uid {info['uid']})  cpu%={info['cpu_pct']}  "
              f"rss={_fmt_kb(info['vm_rss_kb'])}  threads={info['threads']}")

    elif clave == "memoria":
        m = snapshot["memoria"]["datos"].get(pid)
        if not m:
            linea("(sin datos)")
            return
        linea(f"VmSize={_fmt_kb(m['vm_size_kb'])}  VmRSS={_fmt_kb(m['vm_rss_kb'])}  "
              f"VmHWM={_fmt_kb(m['vm_hwm_kb'])}  VmSwap={_fmt_kb(m['vm_swap_kb'])}")
        linea(f"VmData={_fmt_kb(m['vm_data_kb'])}  VmStk={_fmt_kb(m['vm_stk_kb'])}  "
              f"VmExe={_fmt_kb(m['vm_exe_kb'])}  VmLib={_fmt_kb(m['vm_lib_kb'])}")
        linea(f"page faults: minor={m['minflt']}  major={m['majflt']}")
        seg = m.get("segmentos")
        if seg:
            linea(f"segmentos: heap={_fmt_kb(seg['heap'])}  stack={_fmt_kb(seg['stack'])}  "
                  f"texto={_fmt_kb(seg['texto'])}  datos={_fmt_kb(seg['datos'])}  resto={_fmt_kb(seg['resto'])}")
        else:
            linea("segmentos: sin permiso para leer /proc/<pid>/maps")

    elif clave == "fds":
        f = snapshot["fds"]["datos"].get(pid)
        if not f:
            linea("(sin datos o sin permiso)")
            return
        linea(f"total={f['total']}  por tipo: {f['por_tipo']}")
        for d in f["detalle"][:altura]:
            linea(f"  fd {d['fd']:>3} [{d['tipo']:<10}] -> {d['destino']}")

    elif clave == "threads":
        t = snapshot["threads"]["datos"].get(pid)
        if not t:
            linea("(sin datos)")
            return
        linea(f"{'TID':>7} {'ST':>3} {'CPU%':>6} {'CTX_V':>7} {'CTX_I':>7}  COMM")
        for tid, info in t.items():
            linea(f"{tid:>7} {info['estado']:>3} {info['cpu_pct']:>6.1f} "
                  f"{info['ctx_voluntarios']:>7} {info['ctx_involuntarios']:>7}  {info['comm']}")

    elif clave == "senales":
        s = snapshot["senales"]["datos"].get(pid)
        if not s:
            linea("(sin datos)")
            return
        linea(f"bloqueadas:   {', '.join(s['bloqueadas']) or '(ninguna)'}")
        linea(f"ignoradas:    {', '.join(s['ignoradas']) or '(ninguna)'}")
        linea(f"con_handler:  {', '.join(s['con_handler']) or '(ninguna)'}")
        linea(f"pendientes:   {', '.join(s['pendientes']) or '(ninguna)'}")
        linea(f"pend._grupo:  {', '.join(s['pendientes_grupo']) or '(ninguna)'}")

    elif clave == "scheduling":
        sc = snapshot["scheduling"]["datos"].get(pid)
        if not sc:
            linea("(sin datos)")
            return
        linea(f"policy={sc['policy']}  nice={sc['nice']}  priority={sc['priority']}  rt_priority={sc['rt_priority']}")
        linea(f"pgrp(PGID)={sc['pgrp']}  sid(SID)={sc['sid']}  cpus_allowed={sc['cpus_allowed']}")
        linea(f"ctx switches: voluntarios={sc['ctxt_voluntarios']}  involuntarios={sc['ctxt_involuntarios']}")

    elif clave == "sistema":
        s = snapshot["sistema"]["datos"]
        if not s:
            linea("(sin datos)")
            return
        linea(f"CPU global={s.get('cpu_pct', 0)}%   Load: {s.get('load1', 0):.2f} "
              f"{s.get('load5', 0):.2f} {s.get('load15', 0):.2f}")
        linea(f"Mem total={_fmt_kb(s.get('mem_total_kb', 0))}  libre={_fmt_kb(s.get('mem_libre_kb', 0))}  "
              f"disponible={_fmt_kb(s.get('mem_disponible_kb', 0))}")
        linea(f"buffers={_fmt_kb(s.get('mem_buffers_kb', 0))}  cached={_fmt_kb(s.get('mem_cached_kb', 0))}  "
              f"swap={_fmt_kb(s.get('mem_swap_libre_kb', 0))}/{_fmt_kb(s.get('mem_swap_total_kb', 0))}")
        linea(f"procesos={s.get('total_procesos', 0)}  threads_totales={s.get('threads_totales', 0)}  "
              f"zombies={s.get('zombies', 0)}  por_estado={s.get('por_estado', {})}")
        uptime_h = s.get("uptime_seg", 0) / 3600
        linea(f"uptime={uptime_h:.1f}h")

        resumen = snapshot["resumen"]["datos"]
        if resumen:
            top_cpu = sorted(resumen.items(), key=lambda pi: pi[1]["cpu_pct"], reverse=True)[:3]
            top_rss = sorted(resumen.items(), key=lambda pi: pi[1]["vm_rss_kb"], reverse=True)[:3]
            linea("Top 3 CPU%: " + ", ".join(f"{p}:{i['comm']}({i['cpu_pct']}%)" for p, i in top_cpu))
            linea("Top 3 RSS:  " + ", ".join(f"{p}:{i['comm']}({_fmt_kb(i['vm_rss_kb'])})" for p, i in top_rss))