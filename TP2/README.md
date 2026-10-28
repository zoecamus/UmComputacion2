# TP2 — Plataforma de Tareas Distribuidas

**Índice:** [Descripción](#descripción) · [Cómo correr](#cómo-correr) · [Cómo probar](#cómo-probar) · [Arquitectura](#arquitectura) · [Decisiones de diseño](#decisiones-de-diseño) · [Apagado ordenado](#apagado-ordenado) · [Mediciones](#mediciones) · [Pruebas de cancelación y timeouts](#pruebas-de-cancelación-y-timeouts) · [Conceptos del curso aplicados](#conceptos-del-curso-aplicados) · [Limitaciones conocidas](#limitaciones-conocidas) · [Lo que aprendí](#lo-que-aprendí)

## Descripción

La API encola tareas en una `asyncio.Queue` y un pool de N corrutinas las
ejecuta. Los tres tipos de tarea hacen trabajo real: `esperar` (I/O
simulado), `descargar` (I/O de red con `httpx`) y `hashear` (CPU-bound, en un
`ProcessPoolExecutor`). `DELETE` cancela tareas pendientes **y en ejecución**,
y `timeout_s` se aplica: al vencer, la tarea pasa a `fallida` con el motivo.
Con `SIGTERM` o `SIGINT` el servidor se apaga de forma ordenada: deja de
aceptar tareas, espera (con límite) a las que están corriendo y vuelca el
estado a `datos/estado_<timestamp>.json`.

## Cómo correr

```bash
docker compose up --build
```

La API queda en `http://localhost:8000` (documentación interactiva en
`/docs`). Variables de entorno (ver `docker-compose.yml`): `NUM_WORKERS`
(default 3) y `SHUTDOWN_TIMEOUT_S` (default 20, cuánto espera al apagarse).

Los volcados se escriben en `./datos/` del host (un volumen), así sobreviven
aunque el contenedor se recree. La carpeta viaja en el repositorio
(`datos/.gitkeep`); los volcados en sí no se versionan.

## Cómo probar

```bash
# Encolar una tarea (responde al instante con su id)
curl -X POST localhost:8000/tareas \
  -H 'Content-Type: application/json' \
  -d '{"tipo":"esperar","parametros":{"segundos":5}}'

# Ver una tarea (reemplazá el id por el que devolvió el POST)
curl localhost:8000/tareas/ff8ac1d5-218b-4a8f-9cc8-e9651f3b02bd

# Descargar varias URLs a la vez (el resultado trae status, bytes y tiempo por URL)
curl -X POST localhost:8000/tareas \
  -H 'Content-Type: application/json' \
  -d '{"tipo":"descargar","parametros":{"urls":["https://httpbin.org/delay/1","https://example.com"]}}'

# Hash SHA-256 iterado 10 millones de veces (pesado: ~4 s). Mientras corre,
# probá curl localhost:8000/tareas: tiene que responder al instante.
curl -X POST localhost:8000/tareas \
  -H 'Content-Type: application/json' \
  -d '{"tipo":"hashear","parametros":{"texto":"hola","iteraciones":10000000}}'

# Listar, con filtro opcional por estado
curl localhost:8000/tareas
curl "localhost:8000/tareas?estado=ejecutando"

# Cancelar una tarea (pendiente o en ejecución). Reemplazá el id por uno real.
curl -X DELETE localhost:8000/tareas/ff8ac1d5-218b-4a8f-9cc8-e9651f3b02bd

# Timeout: una tarea de 10 s con timeout de 2 s termina "fallida" a los 2 s
curl -X POST localhost:8000/tareas \
  -H 'Content-Type: application/json' \
  -d '{"tipo":"esperar","parametros":{"segundos":10},"timeout_s":2}'

# Estadísticas
curl localhost:8000/estadisticas
```

Para ver el pool trabajando, encolá 5 tareas de 2 segundos y consultá
`/estadisticas` enseguida: con 3 workers van a aparecer 3 `ejecutando` y 2
`pendiente`.

## Arquitectura

```
 Cliente HTTP ── POST / GET / DELETE /tareas ──> API FastAPI (event loop, 1 hilo)
                                                    │ put_nowait(id)      ▲ lee y escribe
                                                    ▼                     │ tareas{id: estado}
                                              asyncio.Queue               │
                                                    │ get()               │
                                                    ▼                     │
                                     N workers (corrutinas) ──────────────┘
                                     create_task(_ejecutar) + asyncio.timeout
                                       │              │                │
                                    esperar        descargar         hashear
                                 asyncio.sleep    httpx (async)   run_in_executor
                                                                       │
                                                                       ▼
                                                      ProcessPoolExecutor (procesos hijos)

 Estados:  pendiente ──> ejecutando ──> completada
                                   ├──> fallida    (error o timeout)
                                   └──> cancelada  (DELETE o apagado)

 SIGTERM / SIGINT ──> apagar(): 503 a POST, espera con límite, vuelca datos/estado_*.json
```

La API, la cola y los workers viven en un solo proceso y un solo hilo (el
event loop de uvicorn). Las tareas de I/O (`esperar`, `descargar`) también
corren en ese hilo, como corrutinas. Solo las tareas CPU-bound (`hashear`)
salen a **procesos hijos**.

## Decisiones de diseño

- **Endpoints `async def` y estado en un `dict` sin Lock.** El event loop
  solo cambia de corrutina en un `await`. Las modificaciones del dict
  (`encolar`, cambios de estado en `_procesar`) no tienen un `await` en el
  medio, así que ninguna otra corrutina puede interrumpirlas a la mitad.
- **`asyncio.Queue` y no `queue.Queue`.** Los workers son corrutinas que
  comparten un único hilo, el del event loop. `asyncio.Queue.get()` es una
  corrutina: si la cola está vacía, el worker se suspende con `await` y el
  event loop sigue atendiendo a los demás. `queue.Queue.get()` es
  bloqueante: dejaría al hilo entero esperando, y como ese hilo es el del
  event loop, se congelaría la API y todos los workers a la vez. Además,
  `queue.Queue` usa locks para ser seguro entre hilos, algo que acá no hace
  falta: todo corre en un hilo y solo se cambia de corrutina en un `await`.
  Es el mismo patrón productor-consumidor que vimos con threads, pero con
  corrutinas.
- **Los workers nacen en el `lifespan` de FastAPI**, porque necesitan el
  event loop de uvicorn ya corriendo.
- **Una tarea que falla no tumba al worker.** `_procesar` captura
  `Exception`, marca la tarea `fallida` con el motivo y el worker sigue con
  la próxima. Además `_worker` tiene una segunda red de seguridad con
  `log.exception`, para que un bug del propio bookkeeping no mate la
  corrutina en silencio y deje al pool con un worker menos.
- **`esperar` usa `asyncio.sleep`**, que cede el control mientras espera.
  Con `time.sleep` el event loop quedaría congelado.
- **`httpx` async y no `requests`.** `requests` es bloqueante: una llamada
  congelaría el event loop (y con él la API y los demás workers) hasta que
  llegue la respuesta. `httpx.AsyncClient` hace `await` en la red y cede el
  control mientras espera. Elegí `httpx` sobre `aiohttp` porque su API es casi
  idéntica a la de `requests` y es más fácil de leer y defender.
- **`asyncio.gather` para las URLs de una tarea.** Arranca todas las
  descargas a la vez y tarda lo que tarda la más lenta. Un `for` con `await`
  adentro las haría una por una (tarda la suma). El parámetro opcional
  `secuencial: true` existe solo para poder medir esa diferencia.
- **Un error de red en una URL no cancela a las demás.** `_bajar` convierte
  la excepción en datos (`"ok": false, "error": ...`). La tarea queda
  `completada` si se bajó al menos una URL, y `fallida` si no se pudo bajar
  ninguna. Un 404 cuenta como respuesta recibida (`ok: true, status: 404`).
- **`hashear` va a un `ProcessPoolExecutor` con `run_in_executor`.** Un
  hash iterado es CPU puro: no tiene ningún punto donde esperar, así que no
  hay `await` que ceda el control y asyncio no puede ayudar. Si corre en el
  event loop, lo congela. Con `run_in_executor` el trabajo se hace en otro
  proceso y el worker hace `await` del resultado, dejando el loop libre.
- **Procesos y no hilos.** `hashlib` sobre entradas chicas (32 bytes por
  vuelta) mantiene el GIL tomado, así que un `ThreadPoolExecutor` compartiría
  el GIL con el event loop. Con procesos, cada tarea tiene su propio
  intérprete y puede correr en paralelo real en otro núcleo. El costo es
  mandar los datos entre procesos (pickle), que acá es despreciable.
- **`hash_iterado` vive en `tareas_cpu.py`**, a nivel raíz, porque el
  executor tiene que serializar la función con pickle para mandarla al otro
  proceso (un método de clase o una lambda no se pueden mandar).
- **Un proceso por worker** (`max_workers = NUM_WORKERS`), así si todos los
  workers hashean a la vez, cada uno tiene su proceso.
- **`bloqueante: true` es un modo "mal a propósito"** que corre el hash
  directo en el event loop. Existe solo para la medición 2.
- **Cada ejecución corre en su propia `asyncio.Task`**, separada del worker.
  `_procesar` lanza `trabajo = create_task(_ejecutar(...))` y hace `await
  trabajo`. Así `DELETE` puede hacer `trabajo.cancel()` sin tocar al worker,
  que sigue vivo para la próxima tarea. Si se cancelara al worker mismo, el
  pool perdería un worker por cada cancelación.
- **`CancelledError` se distingue por el motivo.** Puede llegar a
  `_procesar` porque alguien hizo `DELETE` (la tarea pasa a `cancelada`) o
  porque se está apagando el servidor (se vuelve a lanzar con `raise`). Se
  distingue con un set `_cancelando` que `cancelar()` marca antes de cancelar.
  Hay que capturarla aparte porque hereda de `BaseException`: `except
  Exception` no la agarra.
- **Timeout con `asyncio.timeout(timeout_s)`** (Python 3.11+). Cuenta desde que
  la tarea **empieza a ejecutarse**, no desde que se encoló. Al vencer cancela
  al worker, la cancelación baja a `trabajo` y el `with` la convierte en
  `TimeoutError`: la tarea queda `fallida` con el motivo.
- **Cancelación limpia sin código extra.** Cuando `trabajo` recibe el
  `CancelledError`, los `async with` y `finally` de adentro corren solos:
  `descargar` cierra el `AsyncClient` y sus conexiones, `esperar` sale del
  sleep. No quedan corrutinas huérfanas porque el worker siempre espera
  (`await`) a `trabajo` hasta que termine.
- **`hashear` se manda al executor en pedazos de 500.000 iteraciones.**
  Cancelar el `await` de `run_in_executor` no detiene al proceso hijo si ya
  está calculando: seguiría quemando CPU hasta terminar. El hash iterado se
  puede cortar (el resultado de un pedazo es la entrada del siguiente), y
  entre pedazo y pedazo hay un `await` donde entran la cancelación y el
  timeout. Lo máximo que se desperdicia es un pedazo (~0,2 s). Al cancelar, el
  proceso hijo deja de consumir CPU. 
  Costo: un pequeño sobrecosto por los viajes entre procesos (pickle) en cada
  pedazo.
- **Las señales se atrapan con `loop.add_signal_handler`**, no con
  `signal.signal`. Un handler de `signal.signal` corre interrumpiendo al
  intérprete en un punto cualquiera, donde no es seguro tocar el estado de
  asyncio. `add_signal_handler` en cambio programa la función para que corra
  **dentro del event loop**, como cualquier callback; desde ahí se puede
  crear una task. Reemplaza a los handlers que puso uvicorn, así que el
  apagado lo conducimos nosotros y recién al final le pedimos a uvicorn que
  termine (`server.should_exit = True`). Para tener ese objeto armo
  `uvicorn.Server` a mano en vez de usar `uvicorn.run`.
- **El apagado vive en `Plataforma.apagar`**, que es idempotente (una sola
  vez). El `lifespan` también la llama al terminar, como red de seguridad: si
  uvicorn se apaga por otro camino, igual se vuelca el estado.
- **Se espera con un `asyncio.Event`, no con un bucle de `sleep`.** El Event
  `_libres` se prende cuando `ocupados` llega a 0 y se apaga cuando arranca
  una tarea. `apagar` hace `await _libres.wait()` dentro de
  `asyncio.timeout(límite)`: se despierta apenas termina la última tarea, o al
  vencer el límite.
- **Volcado atómico**: se escribe a un `.tmp` y se renombra con `os.replace`.
  Si algo mata al proceso a mitad de la escritura, no queda un
  `estado_*.json` cortado.
- **Los hijos del `ProcessPoolExecutor` ignoran `SIGINT`**
  (`initializer=ignorar_sigint`). Con Ctrl+C la señal llega a todo el grupo
  de procesos; si los hijos murieran, el pool quedaría roto en pleno
  apagado. Con `docker stop` no hace falta, porque solo recibe la señal el
  proceso principal (PID 1).
- **`stop_grace_period: 30s`** en el compose. Docker manda `SIGTERM` y a los
  10 s (default) `SIGKILL`; con el límite de espera en 20 s no alcanzaría.
- **`DELETE` es síncrono hasta el `cancel()`.** `Plataforma.cancelar` no tiene
  `await`: cambia el estado y cancela la task sin que el event loop pueda
  meter otra corrutina en el medio.

## Apagado ordenado

```bash
# 1. Dejá corriendo algo de trabajo
curl -X POST localhost:8000/tareas -H 'Content-Type: application/json' \
  -d '{"tipo":"esperar","parametros":{"segundos":8}}'

# 2. Pedí el apagado (SIGTERM) desde otra terminal
docker compose stop        # o: docker stop <nombre del contenedor>

# 3. Mirá el volcado y reiniciá para ver que lo reporta
ls datos/
docker compose up
```

Qué pasa al recibir la señal:

1. `POST /tareas` empieza a responder `503` (los `GET` y `DELETE` siguen
   andando, así se puede mirar cómo avanza el apagado: `/estadisticas`
   muestra `"apagando": true`).
2. Las tareas que **ya estaban ejecutándose** pueden terminar, hasta
   `SHUTDOWN_TIMEOUT_S` segundos. Las que seguían en la cola no se arrancan.
3. Si se acaba el límite, se cancelan las que quedan (estado `cancelada`,
   `error: "interrumpida por el apagado del servidor"`).
4. Se escribe `datos/estado_<fecha>_<hora UTC>.json` con las estadísticas y
   todas las tareas, y el servidor termina.
5. Una **segunda señal** durante la espera no espera más: cancela lo que
   quede, vuelca y sale.

Al arrancar, el log informa el volcado más reciente que encuentre.

Ejemplo del log de un apagado (con una tarea de 2 s corriendo):

```
INFO:main:Recibí SIGTERM
INFO:plataforma:Apagado ordenado (SIGTERM): 1 en ejecución, 0 esperando en cola. Espero hasta 5 s.
INFO:plataforma:Estado volcado a datos/estado_20261026_212240.json (17 tareas)
```

## Mediciones

### 1. Descargas: secuencial vs concurrente

Script: `python3 scripts/medir_descargas.py` (con la API levantada). Corre las
dos tareas una después de la otra para que no compitan por la red.

| Entorno | N | Secuencial | Concurrente | Speedup |
|---|---|---|---|---|
| httpbin.org/delay/1 (red real, mi máquina) | 20 | 24.06 s | 2.09 s | 11.5x |

Con 20 descargas de 1 s la ganancia fue de 11,5x y no de 20x. Mi hipótesis
(que no medí por separado) es que influyen el costo de abrir 20 conexiones
TLS a la vez, que httpbin es un servicio público compartido y el trabajo del
event loop. En una red real, cada descarga no es exactamente un `sleep`.

### 2. CPU-bound con y sin executor

Script: `python3 scripts/medir_cpu.py`. Mientras corre una tarea `hashear`
de 10 millones de iteraciones, pide `GET /tareas` en bucle y mide cuánto
tarda cada respuesta.

| Entorno | Modo | Duración del hash | GETs completados | Latencia media | Latencia máxima |
|---|---|---|---|---|---|
| Mi máquina | bloqueante (sin executor) | 3.35 s | 1 | 3355 ms | 3355 ms |
| Mi máquina | con executor | 3.35 s | 66 | 2 ms | 5 ms |


Sin executor, el único `GET` que alcanzó a hacerse quedó trabado todo el
tiempo que duró el hash: la API estuvo congelada. Con executor, la tarea
tarda casi lo mismo, pero la API respondió 66 veces con latencia de
milisegundos. El executor no hace más rápido el hash; hace que no bloquee al
resto.

### 3. Techo del pool

Script: `python3 scripts/medir_techo.py`. Lanza N tareas de golpe, para
distintos N, y mide (con los timestamps que guarda el servidor en cada tarea)
cuánto tarda en terminar todo, cuánto tarda cada tarea desde que se creó, y
cuánto espera en la cola antes de empezar.

**Hipótesis previa (el modelo).** Cada worker ejecuta una tarea a la vez, así
que como mucho W tareas avanzan al mismo tiempo (W = `NUM_WORKERS`). Con
N ≤ W todas arrancan al instante y el total es lo que dura UNA tarea. Con
N > W las sobrantes esperan en la cola: el total sube a `ceil(N/W) x d`
(d = duración de una tarea sola), la latencia crece de forma lineal con N y el
throughput se queda clavado en `W/d` tareas por segundo.

- **Tareas de I/O (`esperar`, `descargar`)**: el techo es W, y es una decisión
  de diseño, no un límite físico. Una corrutina esperando no consume CPU, así
  que subir W sube el techo casi sin costo.
- **Tareas de CPU (`hashear`)**: el techo es `min(W, núcleos)`. Los hashes
  corren en procesos y cada uno necesita un núcleo para avanzar; con más
  procesos que núcleos no hay más trabajo hecho por segundo, solo más procesos
  peleándose por el procesador.

**Cómo reproducirlo** (`nproc` es la cantidad de núcleos de la máquina):

```bash
# A. I/O con 3 workers
docker compose up -d --build && sleep 3
python3 scripts/medir_techo.py --tipo esperar

# B. I/O con 20 workers: el techo se mueve
NUM_WORKERS=20 docker compose up -d && sleep 3
python3 scripts/medir_techo.py --tipo esperar --ns 1,3,12,24,48

# C. CPU con tantos workers como núcleos
NUM_WORKERS=$(nproc) docker compose up -d && sleep 3
python3 scripts/medir_techo.py --tipo hashear --ns 1,4,8,16,24,32,48

# D. CPU con el doble de workers que núcleos
NUM_WORKERS=$(( 2 * $(nproc) )) docker compose up -d && sleep 3
python3 scripts/medir_techo.py --tipo hashear --ns 1,4,8,16,24,32,48

NUM_WORKERS=3 docker compose up -d      # volver al valor por defecto
```

**Resultados en mi máquina** _(16 procesadores lógicos)_

#### A. Tareas de I/O (`esperar`, 1 s), 3 workers

| N tareas | Total (s) | Ideal (s) | Latencia media (s) | Latencia máx (s) | Espera media en cola (s) | Tareas/s |
|---|---|---|---|---|---|---|
| 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 1.0 |
| 2 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 2.0 |
| 3 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 3.0 |
| 4 | 2.00 | 2.00 | 1.25 | 1.99 | 0.25 | 2.0 |
| 6 | 2.00 | 2.00 | 1.50 | 2.00 | 0.50 | 3.0 |
| 9 | 3.01 | 3.00 | 1.99 | 2.99 | 0.99 | 3.0 |
| 12 | 4.00 | 4.00 | 2.49 | 3.99 | 1.49 | 3.0 |
| 24 | 8.01 | 8.00 | 4.48 | 7.97 | 3.48 | 3.0 |
| 48 | 16.01 | 16.00 | 8.47 | 15.94 | 7.47 | 3.0 |

**Lectura de A.** Hasta 3 tareas simultáneas el total es 1 s, lo que dura una
sola. En N=4 salta a 2 s: el techo está en 3, igual que `NUM_WORKERS`. Desde
ahí el total sigue a `ceil(N/3) x 1 s` casi exacto, así que no hay ningún otro
límite: el único cuello de botella es el pool. El throughput queda clavado en
3 tareas/s (`W/d`). Lo que se degrada no es el tiempo de ejecución (cada tarea
sigue durando 1 s) sino la **espera en cola**: la latencia media pasa de 1 s a
8,5 s en N=48 porque 45 tareas esperan su turno.

#### B. Tareas de I/O (`esperar`, 1 s), 20 workers

| N tareas | Total (s) | Ideal (s) | Latencia media (s) | Latencia máx (s) | Espera media en cola (s) | Tareas/s |
|---|---|---|---|---|---|---|
| 1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 1.0 |
| 3 | 1.01 | 1.00 | 1.00 | 1.00 | 0.00 | 3.0 |
| 12 | 1.02 | 1.00 | 1.00 | 1.00 | 0.00 | 11.7 |
| 24 | 2.01 | 2.00 | 1.16 | 1.97 | 0.16 | 12.0 |
| 48 | 3.02 | 3.00 | 1.72 | 2.93 | 0.72 | 15.9 |

**Lectura de B.** Con 20 workers el techo se movió de 3 a 20. El total vuelve a
seguir a `ceil(N/20) x 1 s` (2 s para N=24, 3 s para N=48). Las 48 tareas
pasaron de tardar 16 s (con 3 workers) a 3 s, y la espera media en cola de
7,5 s a 0,7 s, sin ningún costo visible: cada tarea sigue durando 1 s, porque
una corrutina que espera en `asyncio.sleep` casi no consume recursos. El
throughput no llega a 20 tareas/s porque 48 = 20 + 20 + 8: la última tanda
usa solo 8 workers. Subir el techo de las tareas de I/O es barato.

#### C. Tareas de CPU (`hashear`, 2 millones de iteraciones), 16 workers

| N tareas | Total (s) | Ideal (s) | Latencia media (s) | Latencia máx (s) | Espera media en cola (s) | Tareas/s |
|---|---|---|---|---|---|---|
| 1 | 1.61 | 1.61 | 1.61 | 1.61 | 0.00 | 0.6 |
| 4 | 1.60 | 1.61 | 1.59 | 1.60 | 0.00 | 2.5 |
| 8 | 2.20 | 1.61 | 1.87 | 2.19 | 0.00 | 3.6 |
| 16 | 3.13 | 1.61 | 2.77 | 3.08 | 0.00 | 5.1 |
| 24 | 4.89 | 3.22 | 3.43 | 4.84 | 0.85 | 4.9 |
| 32 | 5.91 | 3.22 | 4.09 | 5.83 | 1.39 | 5.4 |
| 48 | 8.66 | 4.83 | 5.40 | 8.54 | 2.73 | 5.5 |

#### D. Tareas de CPU, 32 workers (el doble que los procesadores)

| N tareas | Total (s) | Ideal (s) | Latencia media (s) | Latencia máx (s) | Espera media en cola (s) | Tareas/s |
|---|---|---|---|---|---|---|
| 1 | 1.68 | 1.68 | 1.68 | 1.68 | 0.00 | 0.6 |
| 4 | 1.65 | 1.68 | 1.61 | 1.65 | 0.00 | 2.4 |
| 8 | 2.18 | 1.68 | 1.90 | 2.17 | 0.00 | 3.7 |
| 16 | 3.00 | 1.68 | 2.75 | 2.98 | 0.00 | 5.3 |
| 24 | 4.27 | 3.36 | 3.80 | 4.23 | 0.00 | 5.6 |
| 32 | 5.72 | 3.36 | 4.86 | 5.63 | 0.00 | 5.6 |
| 48 | 8.42 | 5.04 | 6.20 | 8.29 | 1.56 | 5.7 |

**Lectura de C y D.** Acá el límite no es el pool. Con N=4 las tareas corren
en paralelo casi perfecto (1,60 s, igual que una sola). Pero con N=16 y 16
workers todas arrancan sin esperar en cola (espera 0,00 s) y el total es
3,13 s, casi el doble de una tarea sola: se frenan entre sí. El throughput
sube hasta unas 5,5 tareas/s y se aplana, o sea unas 9 veces lo que hace una
tarea sola (1/1,61 = 0,62 tareas/s), no 16.

Mi procesador es un i5-1240P: 12 núcleos físicos (4 de alto rendimiento con
hyperthreading y 8 eficientes) y 16 procesadores lógicos (`lscpu`). Un
procesador lógico no rinde lo que un núcleo completo: dos hilos de un mismo
núcleo se reparten sus recursos, y un núcleo eficiente es más lento que uno
de alto rendimiento. Por eso el throughput máximo equivale a unas 9 tareas
corriendo a la velocidad de una sola y no a 16; es consistente con esa
mezcla de núcleos, aunque no lo medí por separado. Además, en un portátil
todos los núcleos comparten consumo y temperatura, y el servidor y el script
de medición también usan CPU.

Con 32 workers (D) el throughput es igual o apenas mejor (5,7 contra 5,5 en
N=48, dentro del ruido de una sola corrida): duplicar los procesos no dio
más trabajo por segundo, pero tampoco lo empeoró.

#### Conclusión: dónde está el límite y por qué

- **Tareas de I/O:** el límite es `NUM_WORKERS`, una decisión de diseño. Es
  barato moverlo: de 3 a 20 workers bajó 48 tareas de 16 s a 3 s sin costo
  visible.
- **Tareas de CPU:** el límite es el hardware, no el pool. Aun con 16 y 32
  workers el sistema se estancó en unas 5,5 tareas/s, unas 9 veces una tarea
  sola. Poner más procesos que procesadores no ayuda.

## Pruebas de cancelación y timeouts

Probado a mano con `curl` sobre la API corriendo en mi máquina:

| Prueba | Resultado observado |
|---|---|
| `esperar` de 10 s con `timeout_s: 2` | `fallida`, error `TimeoutError: superó el timeout de 2.0 s` |
| `DELETE` sobre un `esperar` de 30 s en ejecución | `cancelada` al instante, con `fin_en` cargado |
| `DELETE` sobre un `hashear` de 100 millones de iteraciones, midiendo con `docker stats` | CPU del contenedor de ~100 % antes del `DELETE` a ~0,2 % dos segundos después: el proceso hijo deja de calcular |

## Conceptos del curso aplicados

- **Event loop de un solo hilo y ausencia de locks (clase 18).** La API, la
  cola y los workers corren en el mismo hilo, y una corrutina solo se
  interrumpe en un `await`. Por eso `Plataforma` modifica el diccionario de
  tareas sin ningún `Lock`: entre una modificación y la siguiente no hay un
  `await` en el medio (`encolar`, los cambios de estado de `_procesar`,
  `cancelar`).
- **Patrón productor-consumidor con corrutinas (clase 21; el mismo patrón
  que vimos con threads).** `POST /tareas` produce ids en una `asyncio.Queue`
  y los N workers (`_worker`) los consumen. A diferencia de `queue.Queue`,
  `get()` es una corrutina: el worker ocioso se suspende sin bloquear el loop.
- **API con FastAPI, validación y ciclo de vida (clase 19).** Los endpoints
  son `async def`; `TareaCrear` (Pydantic) valida el cuerpo y responde 422
  sin código propio; el `lifespan` crea los workers cuando el loop de uvicorn
  ya está andando y los detiene al apagar.
- **I/O asíncrono de red (clase 20).** La tarea `descargar` usa
  `httpx.AsyncClient` y `asyncio.gather` para bajar las URLs a la vez; la
  medición 1 muestra la diferencia con hacerlo en secuencia (24 s contra 2 s).
- **Tareas CPU-bound y executors (clase 21; procesos y GIL de las clases de
  multiprocessing y threading).** `hashear` no tiene ningún punto de espera,
  así que asyncio no puede ayudar: se envía con `run_in_executor` a un
  `ProcessPoolExecutor`, que corre en procesos aparte y no comparte el GIL.
  La medición 2 muestra la API congelada sin executor y respondiendo con él.
- **Cancelación y timeouts (clase 21).** `DELETE` llama a `Task.cancel()`,
  que inyecta `CancelledError` (hereda de `BaseException`, por eso se captura
  aparte) en el punto donde la tarea espera; `asyncio.timeout()` aplica
  `timeout_s` con el mismo mecanismo. Los `async with` y `finally` liberan los
  recursos (por ejemplo el `AsyncClient` de `descargar`) sin código extra.
- **Señales y apagado ordenado (clase de señales).** `main.py` registra
  `SIGTERM` y `SIGINT` con `loop.add_signal_handler`, para que el handler
  corra dentro del event loop; `Plataforma.apagar` deja de aceptar tareas,
  espera con límite y vuelca el estado a JSON. Los procesos hijos ignoran
  `SIGINT` (`ignorar_sigint`) para que Ctrl+C no rompa el pool.
- **Docker y Compose (clases de Docker).** `Dockerfile` y `docker-compose.yml`
  levantan todo con `docker compose up --build`; un volumen (`./datos`) hace
  sobrevivir los volcados, y `stop_grace_period` le da tiempo al apagado
  ordenado antes del `SIGKILL`.

## Limitaciones conocidas

- `NUM_WORKERS` dimensiona a la vez el pool de workers (I/O) y el pool de
  procesos del executor (CPU). Las mediciones muestran que sus techos son
  distintos (I/O: barato de subir; CPU: acotado por el hardware), pero no se
  pueden ajustar por separado.
- Una tarea `hashear` con `bloqueante: true` no se puede cancelar ni
  vencer por timeout: no tiene ningún `await`, así que el event loop no puede
  interrumpirla (y mientras corre, congela la API). Es el punto de ese modo.
- Al cancelar un `hashear`, el proceso hijo termina el pedazo en curso (hasta
  ~0,2 s) antes de liberarse.
- Cancelar un `descargar` corta las conexiones de nuestro lado, pero no
  puede hacer que el servidor remoto deje de trabajar.
- `iteraciones` no tiene tope: un valor enorme ocupa un núcleo por mucho
  tiempo (aunque ahora se puede cancelar).
- `descargar` mantiene además un timeout de red de 10 s por request,
  independiente de `timeout_s`.
- `descargar` acepta cualquier URL http(s), incluidas las de la red interna
  del servidor. En un sistema real habría que restringir destinos.
- La respuesta completa se carga en memoria (`r.content`); para archivos
  enormes habría que usar streaming.
- La cola no tiene límite de tamaño: con muchísimas tareas de golpe la
  memoria crece sin control.
- **El volcado se escribe pero no se vuelve a cargar.** Al arrancar solo se
  informa por log que existe un volcado previo; las tareas no se restauran.
  Las que estaban `pendiente` al apagar se pierden (quedan registradas en el
  JSON, pero no se re-encolan).
- Un `SIGKILL` (o `docker stop` pasado el `stop_grace_period`) no se puede
  atrapar: en ese caso no hay volcado. Por eso `SHUTDOWN_TIMEOUT_S` (20 s)
  tiene que ser menor que `stop_grace_period` (30 s).
- Las tareas interrumpidas por el apagado quedan `cancelada` con un `error`
  que explica el motivo; no hay un estado propio para eso.
- El volcado se escribe con I/O bloqueante. Es un archivo chico, así que
  tarda milisegundos, pero con millones de tareas habría que hacerlo en un
  hilo (`asyncio.to_thread`).
- Los volcados se acumulan en `datos/`; no hay rotación ni limpieza.
- `loop.add_signal_handler` solo existe en Unix. En Windows sin Docker se
  usa el manejo de señales de uvicorn: el volcado igual se hace, pero la
  espera con límite pasa a depender de uvicorn.

## Lo que aprendí

Lo que más me quedó es la diferencia entre una tarea de I/O y una de CPU. Antes
de medirlo, para mí "asíncrono" era sinónimo de "rápido". Las mediciones me
mostraron que no: asyncio sirve cuando hay algo que esperar (las 20 descargas
pasaron de 24 s a 2 s con `gather`), pero un hash puro no tiene dónde ceder el
control, y sin executor la API quedó congelada 3,35 s con un solo `GET`
trabado, contra 5 ms con el executor. El executor no hace más rápido el cálculo;
hace que no bloquee a los demás. Eso lo entendí recién cuando lo vi en mis
propios números.

La parte más difícil fue la cancelación y el apagado. Pensé que alcanzaba con
`task.cancel()`, pero cancelar el `await` de `run_in_executor` no detiene al
proceso hijo, así que tuve que dividir el hash en pedazos para tener puntos
donde cortar, y lo comprobé con `docker stats` (el CPU bajó de ~100 % a ~0,2 %
al cancelar). También aprendí que una cancelación puede llegar por dos motivos
distintos (un `DELETE` o un apagado) y que hay que distinguirlos, y que
`docker stop` mata al contenedor a los 10 s por defecto, por eso el
`stop_grace_period`.