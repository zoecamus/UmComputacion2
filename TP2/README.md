# TP2 — Plataforma de Tareas Distribuidas

> Trabajo en curso. Este README se va completando etapa por etapa.

## Estado actual: etapa 6 (apagado ordenado y volcado a JSON)

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
mkdir -p datos            # solo la primera vez (ver nota abajo)
docker compose up --build
```

La API queda en `http://localhost:8000` (documentación interactiva en
`/docs`). Variables de entorno (ver `docker-compose.yml`): `NUM_WORKERS`
(default 3) y `SHUTDOWN_TIMEOUT_S` (default 20, cuánto espera al apagarse).

Los volcados se escriben en `./datos/` del host (un volumen), así sobreviven
aunque el contenedor se recree. Conviene crear la carpeta vos antes de la
primera corrida: si la crea Docker, queda a nombre de root.

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
 Cliente HTTP
      │  POST /tareas
      ▼
 ┌─────────────┐   put_nowait(id)   ┌─────────────────┐
 │ API FastAPI │ ─────────────────> │  asyncio.Queue  │
 └─────────────┘                    └────────┬────────┘
      ▲                                      │ get()
      │ lee el dict                          ▼
      │                         ┌──────────────────────────┐
      └──────────────────────── │ N workers (corrutinas)   │
           tareas{id: estado}   │ pendiente → ejecutando → │
                                │ completada | fallida     │
                                └──────────────────────────┘
```

Todo vive en un solo proceso y un solo hilo (el event loop de uvicorn).

## Decisiones de diseño

- **Endpoints `async def` y estado en un `dict` sin Lock.** El event loop
  solo cambia de corrutina en un `await`. Las modificaciones del dict
  (`encolar`, cambios de estado en `_procesar`) no tienen un `await` en el
  medio, así que ninguna otra corrutina puede interrumpirlas a la mitad.
- **`asyncio.Queue` y no `queue.Queue`.** `Queue.get()` de asyncio es una
  corrutina: mientras no hay nada, el worker se suspende y cede el control
  al event loop. Un `queue.Queue.get()` bloquearía el hilo entero, o sea la
  API completa. _(A completar con más detalle.)_
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
  timeout. Lo máximo que se desperdicia es un pedazo (~0,2 s). Verifiqué que el
  proceso hijo deja de consumir CPU después de cancelar. Costo: ~5 % más de
  tiempo total por los viajes entre procesos.
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
| Servidor local de prueba (0.5 s por request) | 20 | 10.09 s | 0.57 s | 17.7x |
| httpbin.org/delay/1 (red real, mi máquina) | 20 | 24.06 s | 2.09 s | 11.5x |

El speedup no llega a 20x porque hay costo fijo por conexión y el
scheduling del event loop. Si dependiera solo de la espera, sería ideal.

### 2. CPU-bound con y sin executor

Script: `python3 scripts/medir_cpu.py`. Mientras corre una tarea `hashear`
de 10 millones de iteraciones, pide `GET /tareas` en bucle y mide cuánto
tarda cada respuesta.

| Entorno | Modo | Duración del hash | GETs completados | Latencia media | Latencia máxima |
|---|---|---|---|---|---|
| Pruebas en sandbox | bloqueante (sin executor) | 3.82 s | 1 | 3819 ms | 3819 ms |
| Pruebas en sandbox | con executor | 3.44 s | 67 | 2 ms | 6 ms |
| Mi máquina | bloqueante (sin executor) | 3.35 s | 1 | 3355 ms | 3355 ms |
| Mi máquina | con executor | 3.35 s | 66 | 2 ms | 5 ms |

> Nota: estos números se midieron en la etapa 4, cuando el hash iba al
> executor de una sola vez. En la etapa 5 se divide en pedazos para poder
> cancelarlo; en el sandbox eso hizo que el modo executor tarde ~5 % más
> (4,41 s contra 4,17 s del modo bloqueante, 10 millones de iteraciones) con
> la misma latencia de la API (máx. 6 ms). Los números finales se re-miden en
> la etapa 7.

Sin executor, el único `GET` que alcanzó a hacerse quedó trabado todo el
tiempo que duró el hash: la API estuvo congelada. Con executor, la tarea
tarda casi lo mismo, pero la API respondió 67 veces con latencia de
milisegundos. El executor no hace más rápido el hash; hace que no bloquee al
resto.


## Pruebas de cancelación y timeouts

Probado a mano con `curl` sobre la API corriendo en mi máquina:

| Prueba | Resultado observado |
|---|---|
| `esperar` de 10 s con `timeout_s: 2` | `fallida`, error `TimeoutError: superó el timeout de 2.0 s` |
| `DELETE` sobre un `esperar` de 30 s en ejecución | `cancelada` al instante, con `fin_en` cargado |
| `DELETE` sobre un `hashear` de 100 millones de iteraciones | `cancelada`, `workers_ocupados: 0` en `/estadisticas` |

## Limitaciones conocidas

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
