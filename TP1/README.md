# TP1 — Monitor de Procesos y Threads (Linux, multiprocessing)

## Descripción general

Monitor de procesos de Linux que lee `/proc` directamente (sin `psutil`,
sin `subprocess` a `ps`/`top`) y muestra una TUI interactiva con `curses`:
7 vistas alternables (resumen, memoria, FDs, threads, señales, scheduling,
sistema), navegación con teclado, filtros, pin de proceso, orden dinámico
y ajuste de intervalos de refresco en vivo.

Arquitectura multiproceso real: 7 analizadores corren como `Process`
independientes, cada uno con su propio intervalo de refresco, escribiendo
a un snapshot compartido (`Manager().dict()`) que la TUI lee y pinta.

## Cómo correr

```bash
docker compose up -d --build
docker attach tp1-monitor-1
```

**Importante:** `docker compose up` (sin `-d`) muestra los logs pero NO
conecta el teclado al contenedor — la TUI necesita `docker attach` (o
`docker compose run --rm monitor` como alternativa) para recibir input
interactivo de verdad.

Desde otra terminal, con el monitor corriendo:
```bash
docker exec -it tp1-monitor-1 sh -c 'kill -USR1 1'   # dump del snapshot a JSON
docker exec -it tp1-monitor-1 sh -c 'kill -HUP 1'    # recargar config.json en caliente
docker exec -it tp1-monitor-1 sh -c 'kill -USR2 1'   # toggle modo verbose
```

## Teclas de la TUI

| Tecla | Acción |
|---|---|
| `1`-`7` o `r/m/f/t/s/p/g` | Cambiar de vista |
| ↑ / ↓ | Navegar la lista de procesos |
| `Enter` | Pin / unpin del proceso seleccionado |
| `/` | Filtrar por nombre de comando |
| `u` | Filtrar por usuario |
| `c` | Ciclar orden: PID → CPU% → RSS |
| `+` / `-` | Ajustar el intervalo de la vista activa (respeta el mínimo de cada una) |
| `q` | Salir (shutdown limpio de los 7 analizadores) |
| `h` / `?` | Ayuda en pantalla |

## Arquitectura

```
                         recolector (Process)
                    lee /proc UNA vez por vuelta
                              │
              reparte la misma lista de PIDs vía
           7 multiprocessing.Queue (una por analizador,
              patrón "mailbox": último valor gana)
                              │
        ┌──────┬──────┬──────┼──────┬──────┬──────┐
        ▼      ▼      ▼      ▼      ▼      ▼      ▼
    resumen memoria sistema threads senales fds scheduling
        │      │      │      │      │      │      │
        └──────┴──────┴──────┼──────┴──────┴──────┘
                              ▼
                  Manager().dict()  [snapshot compartido]
                              │
                              ▼
                    display.py (curses)
                 corre en el proceso principal,
                 lee el snapshot y pinta la TUI
```

8 `Process` en total (recolector + 7 analizadores), cada analizador con
su propio intervalo vía `multiprocessing.Value` (resumen 2s, memoria 3s,
sistema 2s, threads 2s, senales 10s, fds 5s, scheduling 10s), y el
recolector con el suyo (1s por defecto).

**Por qué `Queue` con `maxsize=1` y no una ilimitada:** a ningún analizador
le sirve una lista de PIDs vieja si ya hay una más nueva disponible. El
recolector, antes de empujar, vacía la cola con `get_nowait()` y pone el
valor más reciente — así nunca se acumula una cola de "PIDs históricos"
que nadie va a leer.

## Decisiones de diseño

- **`Manager().dict()` y no un `dict` normal**: después de `fork()`, cada
  proceso tiene su propia copia de memoria (copy-on-write). Un dict común
  escrito por un analizador jamás sería visto por el proceso principal:
  son memorias físicas distintas. `Manager` resuelve esto con un proceso
  servidor que centraliza el estado y al que todos hablan por IPC.
- **`multiprocessing.Value` para los intervalos**: mismo problema — un
  intervalo modificable necesita cruzar la frontera de procesos, así que
  no puede ser un float común de Python. Además la TUI (`+`/`-`) y
  `SIGHUP` escriben ese mismo `Value` en caliente, sin reiniciar el
  analizador: como cada uno lee `intervalo.value` en cada vuelta de su
  loop (nunca lo cachea), el cambio se aplica solo en la siguiente vuelta.
- **Cada analizador escribe una clave distinta del snapshot**: evita una
  race condition obvia (dos procesos pisándose la misma escritura) sin
  necesitar un `Lock` explícito para esta versión.
- **Reemplazo del sub-dict completo** (`snapshot["resumen"] = {...}`) en
  vez de mutar `snapshot["resumen"]["datos"][pid] = x`: los proxies de
  `Manager` solo detectan asignaciones directas a sus propias claves, no
  mutaciones anidadas.
- **CPU% (global, por proceso y por thread) por delta de jiffies**: una
  sola lectura de `/proc/stat` o `/proc/<pid>/stat` da un acumulado desde
  el boot/desde que arrancó el proceso, no un porcentaje instantáneo. Cada
  analizador guarda la lectura anterior en memoria local (no en el
  snapshot) y calcula el delta contra la actual.
- **FDs clasificados por tipo y con destino**: `/proc/<pid>/fd/<n>` es un
  symlink; su destino empieza con `socket:`, `pipe:` o `anon_inode:` para
  esos tipos especiales, o con `/` si apunta a un archivo real.
- **Segmentos de memoria desde `/proc/<pid>/maps`**: se agrupan por
  heap/stack/texto/datos/resto según el `pathname` y los permisos de cada
  mapping, sumando tamaños en KB.
- **`config.json` + `SIGHUP`**: los intervalos se leen de `config.json` al
  arrancar; `SIGHUP` vuelve a leer el archivo y actualiza los `Value` en
  caliente, sin reiniciar ningún proceso.
- **`SIGUSR2` como toggle de verbose**: un `multiprocessing.Value("i")`
  compartido entre el handler de señal (que corre en el proceso principal)
  y la función de dibujo de la TUI, que muestra `[VERBOSE]` en el header
  cuando está activo.
- **`docker-compose.yml` sin `pid: host`**: compartir el namespace de PID
  del host hacía que el PID 1 visto desde adentro fuera el `systemd` real
  de la máquina, y el kernel prohíbe mandarle señales arbitrarias al
  proceso init salvo que las tenga manejadas -> `SIGUSR1` fallaba con
  "permission denied". Sin ese flag, `main.py` es el PID 1 de su propio
  contenedor y sí acepta señales.
- **Recolector con patrón "mailbox" (`Queue(maxsize=1)`)**: en vez de una
  cola FIFO ilimitada donde los PIDs se acumularían si un analizador es
  más lento que el recolector, cada cola guarda solo el valor más
  reciente. `ipc.pids_mas_recientes()` vacía la cola con `get_nowait()`
  en loop hasta que no queda nada, devolviendo el último visto — así el
  analizador nunca procesa una lista de PIDs desactualizada ni se bloquea
  esperando al recolector.
- **Bootstrap con `procfs.listar_pids()` directo**: cada analizador hace
  UNA lectura directa de `/proc` antes de entrar al loop principal, por si
  el recolector todavía no publicó su primera lista cuando el analizador
  ya arrancó (son procesos independientes, no hay garantía de orden de
  arranque). Después de esa primera vez, todo pasa por la `Queue`.

## Conceptos del curso aplicados

- **Copy-on-write / fork**: motivó la elección de `Manager`.
- **Formato de `/proc/<pid>/stat`**: `comm` entre paréntesis, puede tener
  espacios adentro; se ubica por primer `(` y último `)`.
- **`/proc/<pid>/cmdline`**: separado por bytes nulos, no espacios.
- **TOCTOU**: un proceso puede desaparecer entre `listar_pids()` y la
  lectura de su `/proc/<pid>/...`; se maneja con `FileNotFoundError` (o
  `PermissionError` en el caso de FDs) en vez de dejar caer el programa.
- **Señales async-signal-safe**: los handlers solo tocan un `Event` o un
  `Value`, sin I/O pesado adentro; el trabajo real ocurre en el loop
  principal o en la próxima vuelta de cada analizador.
- **Por qué no se puede matar al PID 1** con una señal arbitraria salvo
  que la tenga manejada (ver decisión de `pid: host` arriba).
- **Threads como LWPs**: cada thread tiene su propia carpeta en
  `/proc/<pid>/task/<tid>/`, mismo formato de `stat` que un proceso.
- **Máscaras de señales** (`SigBlk`/`SigIgn`/`SigCgt`/`SigPnd`/`ShdPnd`):
  enteros de 64 bits donde el bit `N-1` representa la señal `N`.
- **Context switches voluntarios vs involuntarios**: voluntario = el
  proceso cede la CPU (ej. espera I/O); involuntario = el scheduler se la
  quita porque se acabó el quantum.
- **Scheduling**: `nice`, `priority`, `policy` (`SCHED_OTHER`/`FIFO`/`RR`/
  ...), RT priority y CPU affinity (`cpus_allowed`), todos leídos de
  `/proc/<pid>/stat` y `/proc/<pid>/status`.
- **Segmentos de memoria virtual**: heap, stack, texto (código) y datos,
  identificables en `/proc/<pid>/maps` por su `pathname` y permisos.

## Limitaciones conocidas

- No hay `Lock` explícito porque cada analizador escribe una clave
  distinta del snapshot.
- `SIGWINCH` (resize de terminal) no tiene handler explícito — `curses`
  maneja el resize de forma básica internamente, pero no se probó a fondo.

## Cómo testear

```bash
docker compose up -d --build
docker attach tp1-monitor-1
```
Probar cada tecla de la tabla de arriba dentro de la TUI. Para salir del
`attach` sin matar el contenedor: `Ctrl+P` seguido de `Ctrl+Q` (o
simplemente `q` para cerrar el monitor entero).

Desde otra terminal, con el monitor corriendo:
```bash
docker exec -it tp1-monitor-1 sh -c "kill -USR1 1"
docker exec -it tp1-monitor-1 ls /app    # debería aparecer dump_<timestamp>.json
docker cp tp1-monitor-1:/app/dump_<timestamp>.json ./dump.json
python3 -c "import json; print(list(json.load(open('dump.json')).keys()))"
# debería imprimir las 7 claves: resumen, memoria, sistema, threads, senales, fds, scheduling
```

Para probar `SIGHUP`: editar `config.json` (cambiar algún intervalo),
correr `kill -HUP 1` dentro del contenedor, y verificar en la TUI (línea 2
del header) que el intervalo de esa vista cambió sin reiniciar nada.

Para probar `SIGUSR2`: `kill -USR2 1` y verificar que aparece `[VERBOSE]`
en el header de la TUI; repetir para que desaparezca.