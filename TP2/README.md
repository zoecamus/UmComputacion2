# TP2 — Plataforma de Tareas Distribuidas

> Trabajo en curso. Este README se va completando etapa por etapa.

## Estado actual: etapa 3 (tarea `descargar`)

La API encola tareas en una `asyncio.Queue` y un pool de N corrutinas las
ejecuta. Hacen trabajo real los tipos `esperar` y `descargar` (descarga
concurrente de varias URLs con `httpx`). `hashear` se acepta pero termina
`fallida` ("todavía no está implementado").

## Cómo correr

```bash
docker compose up --build
```

La API queda en `http://localhost:8000` (documentación interactiva en
`/docs`). La cantidad de workers se configura con la variable de entorno
`NUM_WORKERS` (default 3, ver `docker-compose.yml`).

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

# Listar, con filtro opcional por estado
curl localhost:8000/tareas
curl "localhost:8000/tareas?estado=ejecutando"

# Cancelar (solo tareas pendientes por ahora)
curl -X DELETE localhost:8000/tareas/<id>

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
- **`DELETE` de una tarea en ejecución responde 409** hasta que
  implementemos la cancelación real (etapa 5), en vez de simular que anduvo.

## Mediciones

### 1. Descargas: secuencial vs concurrente

Script: `python3 scripts/medir_descargas.py` (con la API levantada). Corre las
dos tareas una después de la otra para que no compitan por la red.

| Entorno | N | Secuencial | Concurrente | Speedup |
|---|---|---|---|---|
| Servidor local de prueba (0.5 s por request) | 20 | 10.09 s | 0.57 s | 17.7x |
| httpbin.org/delay/1 (red real) | 20 | 24.06 s | 2.09 s | 11.5x |

El speedup no llega a 20x porque hay costo fijo por conexión y el
scheduling del event loop. Si dependiera solo de la espera, sería ideal.

## Limitaciones conocidas

- Solo `esperar` y `descargar` están implementadas; `hashear` falla con un
  mensaje claro.
- `timeout_s` se guarda en la tarea pero todavía no se aplica. Mientras
  tanto `descargar` usa un timeout fijo de 10 s por request.
- `descargar` acepta cualquier URL http(s), incluidas las de la red interna
  del servidor. En un sistema real habría que restringir destinos.
- La respuesta completa se carga en memoria (`r.content`); para archivos
  enormes habría que usar streaming.
- No se puede cancelar una tarea en ejecución (409).
- La cola no tiene límite de tamaño: con muchísimas tareas de golpe la
  memoria crece sin control.
- Al apagar el servidor se cancelan los workers sin esperar y no se vuelca
  el estado a JSON (etapa 6).
