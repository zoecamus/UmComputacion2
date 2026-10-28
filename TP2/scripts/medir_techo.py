import argparse
import json
import math
import time
import urllib.request

FINALES = ("completada", "fallida", "cancelada")


def pedir(api, metodo, ruta, cuerpo=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(
        api + ruta, data=datos, method=metodo,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def correr_lote(api, tipo, parametros, n):
    """Encola n tareas lo más rápido posible y espera a que terminen todas."""
    ids = {
        pedir(api, "POST", "/tareas",
              {"tipo": tipo, "parametros": parametros, "timeout_s": 300})["id"]
        for _ in range(n)
    }
    terminadas = {}
    while len(terminadas) < n:
        for t in pedir(api, "GET", "/tareas"):
            if t["id"] in ids and t["estado"] in FINALES:
                terminadas[t["id"]] = t
        if len(terminadas) < n:
            time.sleep(0.1)
    return list(terminadas.values())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--tipo", choices=["esperar", "hashear"], default="esperar")
    p.add_argument("--duracion", type=float, default=1.0,
                   help="segundos de cada tarea 'esperar'")
    p.add_argument("--iteraciones", type=int, default=2_000_000,
                   help="iteraciones de cada tarea 'hashear'")
    p.add_argument("--ns", default="1,2,3,4,6,9,12,24,48",
                   help="cantidades de tareas simultáneas a probar")
    a = p.parse_args()

    if a.tipo == "esperar":
        parametros = {"segundos": a.duracion}
    else:
        parametros = {"texto": "techo", "iteraciones": a.iteraciones}

    est = pedir(a.api, "GET", "/estadisticas")
    workers, cpus = est["workers_total"], est["cpus"]
    # Cuántas tareas pueden avanzar de verdad al mismo tiempo:
    #  - esperar (I/O): las limita la cantidad de workers.
    #  - hashear (CPU): además las limitan los núcleos reales.
    techo = workers if a.tipo == "esperar" else min(workers, cpus)

    print(f"Tipo: {a.tipo} | workers: {workers} | núcleos (cpus): {cpus} | "
          f"techo teórico de paralelismo: {techo}\n")
    print("| N tareas | Total (s) | Ideal (s) | Latencia media (s) | "
          "Latencia máx (s) | Espera media en cola (s) | Tareas/s |")
    print("|---|---|---|---|---|---|---|")

    base = None  # duración de UNA tarea sola, medida con N=1
    for n in [int(x) for x in a.ns.split(",")]:
        ts = correr_lote(a.api, a.tipo, parametros, n)
        malas = [t for t in ts if t["estado"] != "completada"]
        if malas:
            raise SystemExit(f"N={n}: {len(malas)} tareas no completaron "
                             f"({malas[0]['estado']}: {malas[0]['error']})")
        total = max(t["fin_en"] for t in ts) - min(t["creada_en"] for t in ts)
        lat = [t["fin_en"] - t["creada_en"] for t in ts]
        espera = [t["inicio_en"] - t["creada_en"] for t in ts]
        if base is None and n == 1:
            base = total
        # Sin la medición de una tarea sola (N=1) no hay con qué comparar.
        ideal = f"{math.ceil(n / techo) * base:.2f}" if base else "-"
        print(f"| {n} | {total:.2f} | {ideal} | {sum(lat)/n:.2f} | "
              f"{max(lat):.2f} | {sum(espera)/n:.2f} | {n/total:.1f} |")

    if base:
        print(f"\nIdeal = ceil(N / {techo}) x {base:.2f} s (lo que tardaría una "
              f"tarea sola). Si 'Total' se separa de 'Ideal', hay otro límite.")
    else:
        print("\n(Para ver la columna 'Ideal', incluí N=1 al principio de --ns.)")


if __name__ == "__main__":
    main()