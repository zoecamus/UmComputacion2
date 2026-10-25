"""Medición 2 del TP2: tarea CPU-bound SIN executor vs CON executor.

Mientras una tarea `hashear` corre, el script le pide GET /tareas a la API
una y otra vez y anota cuánto tarda cada respuesta. Si el event loop está
bloqueado, esas respuestas se demoran.

    python3 scripts/medir_cpu.py
    python3 scripts/medir_cpu.py --iteraciones 10000000
"""
import argparse
import json
import time
import urllib.request


def pedir(api, metodo, ruta, cuerpo=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(
        api + ruta, data=datos, method=metodo,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def medir(api, iteraciones, bloqueante):
    tarea = pedir(api, "POST", "/tareas", {
        "tipo": "hashear",
        "parametros": {
            "texto": "hola", "iteraciones": iteraciones, "bloqueante": bloqueante,
        },
        "timeout_s": 120,
    })
    latencias = []
    while True:
        t0 = time.perf_counter()
        todas = pedir(api, "GET", "/tareas")
        latencias.append(time.perf_counter() - t0)
        mia = next(t for t in todas if t["id"] == tarea["id"])
        if mia["estado"] in ("completada", "fallida", "cancelada"):
            break
        time.sleep(0.05)
    if mia["estado"] != "completada":
        raise SystemExit(f"la tarea terminó {mia['estado']}: {mia['error']}")
    return mia["resultado"]["tiempo_s"], latencias


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--iteraciones", type=int, default=10_000_000)
    a = p.parse_args()

    print(f"{'modo':<12}{'tarea (s)':>11}{'GETs':>7}{'lat. media':>13}{'lat. máx':>11}")
    for modo, bloqueante in (("bloqueante", True), ("executor", False)):
        tiempo, lat = medir(a.api, a.iteraciones, bloqueante)
        media = sum(lat) / len(lat) * 1000
        maximo = max(lat) * 1000
        print(f"{modo:<12}{tiempo:>11.2f}{len(lat):>7}{media:>10.0f} ms{maximo:>8.0f} ms")


if __name__ == "__main__":
    main()