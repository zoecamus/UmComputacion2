# TP1 — Monitor de Procesos (versión reducida)

## Descripción general

Monitor de procesos de Linux que lee `/proc` directamente (sin `psutil`) y
muestra en terminal: lista de procesos con PID/PPID/estado/threads,
memoria por proceso, threads (LWPs) con context switches, señales
decodificadas (bloqueadas/ignoradas/con handler/pendientes), y
estadísticas globales de CPU/memoria/load average.
Arquitectura multiproceso: un proceso por cada dimensión de datos, todos
escribiendo a un snapshot compartido que el proceso principal imprime.

## Cómo correr

```bash
docker compose up --build
```

Dentro, Ctrl+C corta limpio. Desde otra terminal:
```bash
docker exec -it <nombre_del_container> sh -c 'kill -USR1 1'   # dump del snapshot a JSON
```

## Arquitectura

```
              Manager().dict()  [snapshot compartido]
       resumen | memoria | sistema | threads | senales
          ▲        ▲         ▲         ▲          ▲
          │        │         │         │          │
    ┌─────┴──┐┌────┴───┐┌────┴───┐┌────┴───┐┌─────┴────┐
    │resumen ││memoria ││sistema ││threads ││ senales  │  <- 5 Process
    │cada 2s ││cada 3s ││cada 2s ││cada 2s ││cada 10s  │     independientes,
    └────────┘└────────┘└────────┘└────────┘└──────────┘     cada uno con
                             │                                su intervalo
                             ▼                            (multiprocessing.Value)
                       main.py imprime
                       el snapshot cada 1s
```

## Decisiones de diseño

- **`Manager().dict()` y no un `dict` normal**: después de `fork()`, cada
  proceso tiene su propia copia de memoria (copy-on-write). Un dict común
  escrito por un analizador jamás sería visto por el proceso principal:
  son memorias físicas distintas. `Manager` resuelve esto con un proceso
  servidor que centraliza el estado y al que todos hablan por IPC.
- **`multiprocessing.Value` para los intervalos**: mismo problema — un
  intervalo modificable necesita cruzar la frontera de procesos, así que
  no puede ser un float común de Python.
- **Cada analizador escribe una clave distinta del snapshot**
  (`resumen`, `memoria`, `sistema`), nunca la misma clave que otro. Esto
  evita una race condition obvia (dos procesos pisándose la misma
  escritura) sin necesitar un `Lock` explícito para esta versión.
- **Reemplazo del sub-dict completo** (`snapshot["resumen"] = {...}`) en
  vez de mutar `snapshot["resumen"]["datos"][pid] = x`: los proxies de
  `Manager` solo detectan asignaciones directas a sus propias claves, no
  mutaciones anidadas.
- **CPU% global por delta de jiffies**: una sola lectura de `/proc/stat`
  da un acumulado desde el boot, no un porcentaje instantáneo. Se necesita
  comparar dos lecturas separadas por el intervalo de refresco.
- **Threads vía `/proc/<pid>/task/<tid>/`**: cada TID tiene su propio
  `stat` con el mismo formato que el `stat` de un proceso — por eso
  `leer_stat_thread` reutiliza la misma lógica de parseo de `comm` entre
  paréntesis que ya usábamos para procesos.
- **Decodificación de máscaras de señales (`SigBlk`, etc.)**: son enteros
  de 64 bits en hexadecimal donde el bit `N-1` representa la señal número
  `N` (bit 0 = señal 1 = `SIGHUP`). Se recorre cada bit con
  `valor & (1 << (n-1))` y se traduce el número a nombre con el módulo
  `signal` de la stdlib (`signal.Signals(n).name`).
- **Intervalo de señales más largo (10s) que el de resumen (2s)**: leer y
  decodificar 5 máscaras de 64 bits por proceso es más costoso que leer
  un par de campos de `stat`; además esos datos cambian con mucha menos
  frecuencia que el estado o el CPU% de un proceso.

## Conceptos del curso aplicados

- **Copy-on-write / fork**: motivó la elección de `Manager` (Clase 4, 9).
- **Formato de `/proc/<pid>/stat`**: `comm` entre paréntesis, puede tener
  espacios adentro; se ubica por primer `(` y último `)` (Clase 3).
- **`/proc/<pid>/cmdline`**: separado por bytes nulos, no espacios (Clase 3).
- **TOCTOU**: un proceso puede desaparecer entre `listar_pids()` y la
  lectura de su `/proc/<pid>/...`; se maneja con `FileNotFoundError` en
  vez de dejar caer el programa (Clase 3-4).
- **Señales async-signal-safe**: los handlers de `SIGINT`/`SIGTERM` solo
  setean un `Event`, sin hacer I/O adentro del handler; el trabajo pesado
  (join de procesos) ocurre en el loop principal (Clase 6).
- **Threads como LWPs (light-weight processes)**: cada thread de un
  proceso tiene su propia entrada en `/proc/<pid>/task/<tid>/`, con su
  propio estado y sus propios context switches — el kernel los trata como
  entidades schedulables casi idénticas a un proceso, solo que comparten
  memoria (Clase 10).
- **Máscaras de señales (`SigBlk`/`SigIgn`/`SigCgt`/`SigPnd`/`ShdPnd`)**:
  bloqueada ≠ ignorada ≠ manejada — una señal bloqueada queda pendiente
  hasta desbloquearse, una ignorada se descarta sin efecto, y una con
  handler dispara la función instalada por el proceso (Clase 6).
- **Context switches voluntarios vs involuntarios**: un voluntario ocurre
  cuando el proceso cede la CPU (ej: espera I/O); un involuntario ocurre
  cuando el scheduler se la quita porque se le acabó el quantum — por eso
  procesos CPU-bound tienden a acumular más involuntarios (Clase 10).

## Limitaciones conocidas

Esta es una versión **recortada por tiempo**, no la especificación completa:

- 5 de los 7 analizadores obligatorios (resumen, memoria, sistema,
  threads, señales). Faltan: FDs y scheduling.
- No hay TUI real con vistas alternables ni navegación por teclado — solo
  impresión secuencial en terminal.
- Faltan `SIGHUP` (reload de config) y `SIGWINCH`.
- No hay `Lock` explícito porque cada analizador escribe una clave
  distinta; si se agregaran más analizadores escribiendo la misma clave,
  haría falta uno.
- `config.json` no está implementado (intervalos hardcodeados).
- El `docker-compose.yml` **no** usa `pid: host`: el monitor ve solo los
  procesos dentro de su propio namespace (su propio proceso + los 3
  analizadores hijos), no los procesos reales de la máquina host. Probé
  primero con `pid: host` para ver el sistema completo, pero descubrí que
  eso convierte al PID 1 visto desde adentro en el `systemd` real del
  host, y el kernel prohíbe mandarle señales arbitrarias al proceso init
  salvo que las tenga manejadas — lo cual rompía `SIGUSR1`. Prioricé que
  las señales funcionaran correctamente por sobre ver el sistema completo.

## Cómo testear

```bash
docker compose up --build
# en otra terminal, mientras corre:
docker exec -it <nombre_del_container> sh -c "kill -USR1 1"
docker exec -it <nombre_del_container> ls /app    # debería aparecer dump_<timestamp>.json
```
El nombre del container se obtiene con `docker ps` (columna `NAMES`). El
dump se guarda en `/app` (el `WORKDIR` del Dockerfile), no en `/app/src`,
porque el proceso corre con ese directorio de trabajo.
Comparar la tabla de PIDs impresa por el monitor contra `ps -eLf` corrido
dentro del mismo contenedor (`docker exec -it <container> ps -eLf`, si `ps`
estuviera instalado) o contra los procesos que el propio contenedor genera
(el monitor solo ve los procesos de su propio namespace, no los del host).