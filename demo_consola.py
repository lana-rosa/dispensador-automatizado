"""Demostración en consola del flujo completo (usa una BD temporal, no toca dispensador.db).

    python3 demo_consola.py
"""
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import api

reloj = {"t": datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)}
carpeta = tempfile.TemporaryDirectory()
cli = TestClient(api.crear_app(Path(carpeta.name) / "demo.db", reloj=lambda: reloj["t"]))


def titulo(texto):
    print(f"\n{'=' * 70}\n{texto}\n{'=' * 70}")


def pedir(items):
    return cli.post("/pedidos", json={"cliente_nombre": "Ana", "cliente_contacto": "ana@correo.co",
                                      "items": items}).json()


def pin_de(pid):
    return re.findall(r"PIN: (\d+)", " ".join(n["mensaje"] for n in
                                              cli.get(f"/pedidos/{pid}/notificaciones").json()))[-1]


def verificar(pid, peso):
    cli.post(f"/maquina/pedidos/{pid}/llegada")
    return cli.post(f"/maquina/pedidos/{pid}/verificacion",
                    json={"peso_medido_g": peso, "foto_ruta": f"fotos/pedido{pid}.jpg"}).json()


titulo("P1 - Pedido válido (2 Arduino + 1 kit de resistencias) y PIN correcto")
p = pedir([{"sku": "ARD-UNO", "cantidad": 2}, {"sku": "KIT-RES", "cantidad": 1}])
print(f"Pedido #{p['id']}: {p['estado']} en casillero {p['casillero']}, peso teórico {p['peso_teorico_g']} g")
print("Verificación:", verificar(p["id"], 140.5))
pin = pin_de(p["id"])
print("Notificación al cliente:", cli.get(f"/pedidos/{p['id']}/notificaciones").json()[-1]["mensaje"])
r = cli.post(f"/casilleros/{p['casillero']}/abrir", json={"pin": pin})
print("Digita el PIN correcto ->", r.status_code, r.json())

titulo("P2 - PIN incorrecto")
p2 = pedir([{"sku": "PROTO-830", "cantidad": 1}])
verificar(p2["id"], 60.0)
real = pin_de(p2["id"])
malo = "000000" if real != "000000" else "111111"
r = cli.post(f"/casilleros/{p2['casillero']}/abrir", json={"pin": malo})
print("Digita un PIN falso ->", r.status_code, r.json())

titulo("P6 - El peso no coincide: sin PIN y alerta de Error de Despacho")
p6 = pedir([{"sku": "CAUTIN-30", "cantidad": 1}])
print(f"Pedido #{p6['id']} (cautín, 130 g teóricos) en casillero {p6['casillero']}")
print("Verificación con 98 g:", verificar(p6["id"], 98.0))
print("Notificación:", cli.get(f"/pedidos/{p6['id']}/notificaciones").json()[-1]["mensaje"])
for a in cli.get("/admin/alertas").json():
    if a["tipo"] == "ERROR_DESPACHO":
        print("ALERTA AL ADMIN:", a["mensaje"])

titulo("P3 - Todos los casilleros ocupados: el pedido entra en cola")
while True:
    extra = pedir([{"sku": "ARD-UNO", "cantidad": 1}])
    if extra["estado"] == "EN_COLA":
        break
resumen = cli.get("/admin/casilleros").json()
print({c["codigo"]: c["estado"] for c in resumen["casilleros"]})
print(f"Pedido #{extra['id']}: {extra['estado']}, posición {extra['posicion_en_cola']}")
print("Notificación:", cli.get(f"/pedidos/{extra['id']}/notificaciones").json()[0]["mensaje"])

titulo("P5 - PIN no reclamado en 24 h: caduca y el casillero pasa a la cola")
print(f"El pedido #{p2['id']} (P2) sigue con su PIN sin reclamar en {p2['casillero']}.")
reloj["t"] += timedelta(hours=24, seconds=1)
print("Barrido de caducidad ->", cli.post("/admin/caducar-vencidos").json())
print(f"Pedido #{p2['id']}:", cli.get(f"/pedidos/{p2['id']}").json()["estado"])
print(f"Pedido en cola #{extra['id']} ahora:",
      {k: cli.get(f"/pedidos/{extra['id']}").json()[k] for k in ("estado", "casillero")})
print("Stock de PROTO-830 (volvió a inventario):",
      next(x for x in cli.get("/admin/inventario").json() if x["sku"] == "PROTO-830"))

titulo("Trazabilidad del pedido #1 (reserva -> entrega)")
for e in cli.get("/pedidos/1/trazabilidad").json():
    print(f"  {e['creado_en']}  {e['tipo']:<18} {e['detalle'] or ''}")

carpeta.cleanup()
