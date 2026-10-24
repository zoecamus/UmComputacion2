"""Medición 1 del TP2: N descargas secuenciales vs N descargas concurrentes.

Usa solo la librería estándar, así corre en tu máquina sin instalar nada.
Necesita la API levantada (docker compose up --build).

    python3 scripts/medir_descargas.py
    python3 scripts/medir_descargas.py --n 20 --url https://httpbin.org/delay/1

Las dos tareas se corren UNA DESPUÉS DE LA OTRA (no a la vez), para que no
compitan por la red y la comparación sea justa.
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
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def correr(api, urls, secuencial):
    tarea = pedir(api, "POST", "/tareas", {
        "tipo": "descargar",
        "parametros": {"urls": urls, "secuencial": secuencial},
        "timeout_s": 120,
    })
    while True:
        t = pedir(api, "GET", f"/tareas/{tarea['id']}")
        if t["estado"] in ("completada", "fallida", "cancelada"):
            return t
        time.sleep(0.2)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--url", default="https://httpbin.org/delay/1")
    a = p.parse_args()

    urls = [a.url] * a.n
    tiempos = {}
    for modo, secuencial in (("secuencial", True), ("concurrente", False)):
        print(f"Corriendo {a.n} descargas en modo {modo}...")
        t = correr(a.api, urls, secuencial)
        if t["estado"] != "completada":
            print(f"  la tarea terminó {t['estado']}: {t['error']}")
            return
        r = t["resultado"]
        tiempos[modo] = r["tiempo_total_s"]
        print(f"  {r['exitosas']}/{r['total_urls']} OK en {r['tiempo_total_s']} s")

    print()
    print(f"Secuencial:  {tiempos['secuencial']} s")
    print(f"Concurrente: {tiempos['concurrente']} s")
    print(f"Speedup:     {tiempos['secuencial'] / tiempos['concurrente']:.1f}x")


if __name__ == "__main__":
    main()