"""Pruebas de la API con los casos del documento (P1, P2, P3, P5, P6).
P4 (atasco) se prueba en el paso 3, cuando exista el sensor y la máquina de estados."""
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api  # noqa: E402
import db  # noqa: E402

T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


class RelojFalso:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def avanzar(self, **kw):
        self.t += timedelta(**kw)


class BaseApi(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.ruta = Path(self._dir.name) / "test.db"
        self.reloj = RelojFalso()
        self.cli = TestClient(api.crear_app(self.ruta, reloj=self.reloj))

    def tearDown(self):
        self.cli.close()
        self._dir.cleanup()

    # ---- ayudas
    def pedir(self, items=None, esperado=201):
        items = items or [{"sku": "ARD-UNO", "cantidad": 1}]
        r = self.cli.post("/pedidos", json={"cliente_nombre": "Ana", "cliente_contacto": "ana@x.co",
                                            "items": items})
        self.assertEqual(r.status_code, esperado, r.text)
        return r.json()

    def llevar_y_verificar(self, pedido_id, peso):
        self.assertEqual(self.cli.post(f"/maquina/pedidos/{pedido_id}/llegada").status_code, 200)
        r = self.cli.post(f"/maquina/pedidos/{pedido_id}/verificacion",
                          json={"peso_medido_g": peso, "foto_ruta": f"fotos/p{pedido_id}.jpg"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def pin_de(self, pedido_id):
        textos = [n["mensaje"] for n in self.cli.get(f"/pedidos/{pedido_id}/notificaciones").json()]
        for t in reversed(textos):  # el más reciente
            m = re.search(r"PIN: (\d+)", t) or re.search(r"Nuevo PIN .*?: (\d+)", t)
            if m:
                return m.group(1)
        self.fail(f"No hay PIN en las notificaciones: {textos}")

    def stock(self, sku):
        return next(p for p in self.cli.get("/admin/inventario").json() if p["sku"] == sku)

    def casillero(self, codigo):
        return next(c for c in self.cli.get("/admin/casilleros").json()["casilleros"]
                    if c["codigo"] == codigo)


class PruebasFlujo(BaseApi):
    def test_P1_pedido_valido_y_pin_correcto_abre(self):
        # Carrito con dos productos distintos -> UN pedido, UN casillero, un solo peso.
        pedido = self.pedir([{"sku": "ARD-UNO", "cantidad": 2}, {"sku": "KIT-RES", "cantidad": 1}])
        self.assertEqual(pedido["estado"], "ASIGNADO")
        self.assertEqual(pedido["peso_teorico_g"], 2 * 25 + 90)
        self.assertEqual(self.stock("ARD-UNO")["stock_reservado"], 2)

        v = self.llevar_y_verificar(pedido["id"], peso=141.0)  # dentro de tolerancia (±2.8 g)
        self.assertTrue(v["aprobado"])
        pin = self.pin_de(pedido["id"])
        self.assertEqual(len(pin), 6)

        r = self.cli.post(f"/casilleros/{pedido['casillero']}/abrir", json={"pin": pin})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["abierto"])
        self.assertEqual(self.cli.get(f"/pedidos/{pedido['id']}").json()["estado"], "ENTREGADO")
        self.assertEqual(self.casillero(pedido["casillero"])["estado"], "LIBRE")
        self.assertEqual(self.stock("ARD-UNO")["stock_reservado"], 0)
        self.assertEqual(self.stock("ARD-UNO")["stock_disponible"], 10)  # 12 - 2 entregadas
        tipos = [e["tipo"] for e in self.cli.get(f"/pedidos/{pedido['id']}/trazabilidad").json()]
        self.assertIn("CERRADURA_ABIERTA", tipos)

    def test_P2_pin_incorrecto_no_abre(self):
        pedido = self.pedir()
        self.llevar_y_verificar(pedido["id"], 25.0)
        pin = self.pin_de(pedido["id"])
        malo = "000000" if pin != "000000" else "111111"
        r = self.cli.post(f"/casilleros/{pedido['casillero']}/abrir", json={"pin": malo})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(r.json()["abierto"])
        self.assertEqual(r.json()["intentos_restantes"], 2)
        self.assertEqual(self.cli.get(f"/pedidos/{pedido['id']}").json()["estado"], "LISTO_RETIRO")
        tipos = [e["tipo"] for e in self.cli.get(f"/pedidos/{pedido['id']}/trazabilidad").json()]
        self.assertNotIn("CERRADURA_ABIERTA", tipos)

    def test_pin_de_otro_casillero_no_sirve(self):
        a, b = self.pedir(), self.pedir()
        self.llevar_y_verificar(a["id"], 25.0)
        self.llevar_y_verificar(b["id"], 25.0)
        r = self.cli.post(f"/casilleros/{b['casillero']}/abrir", json={"pin": self.pin_de(a["id"])})
        self.assertEqual(r.status_code, 403)

    def test_tres_pines_malos_bloquean_y_alertan_y_el_admin_reemite(self):
        pedido = self.pedir()
        self.llevar_y_verificar(pedido["id"], 25.0)
        pin = self.pin_de(pedido["id"])
        malo = "000000" if pin != "000000" else "111111"
        url = f"/casilleros/{pedido['casillero']}/abrir"
        for _ in range(3):
            r = self.cli.post(url, json={"pin": malo})
        self.assertEqual(r.json()["motivo"], "PIN_BLOQUEADO")
        # Aun con el PIN correcto, ya no abre.
        self.assertEqual(self.cli.post(url, json={"pin": pin}).status_code, 403)
        self.assertIn("PIN_BLOQUEADO", [a["tipo"] for a in self.cli.get("/admin/alertas").json()])
        self.assertEqual(self.cli.post(f"/admin/pedidos/{pedido['id']}/reemitir-pin").status_code, 200)
        self.assertEqual(self.cli.post(url, json={"pin": self.pin_de(pedido["id"])}).status_code, 200)

    def test_P3_casilleros_llenos_entra_en_cola_y_se_despacha_al_liberarse(self):
        primeros = [self.pedir() for _ in range(6)]  # hay 6 casilleros
        self.assertTrue(all(p["estado"] == "ASIGNADO" for p in primeros))
        extra = self.pedir()
        self.assertEqual(extra["estado"], "EN_COLA")
        self.assertEqual(extra["posicion_en_cola"], 1)
        self.assertEqual(self.cli.get("/admin/casilleros").json()["ocupacion_pct"], 100.0)
        self.assertEqual(self.stock("ARD-UNO")["stock_reservado"], 7)  # la cola también reserva

        # Se entrega el primero y su casillero pasa solo al pedido en cola.
        p1 = primeros[0]
        self.llevar_y_verificar(p1["id"], 25.0)
        self.cli.post(f"/casilleros/{p1['casillero']}/abrir", json={"pin": self.pin_de(p1["id"])})
        despues = self.cli.get(f"/pedidos/{extra['id']}").json()
        self.assertEqual(despues["estado"], "ASIGNADO")
        self.assertEqual(despues["casillero"], p1["casillero"])

    def test_cola_respeta_orden_de_llegada(self):
        for _ in range(6):
            self.pedir()
        e1, e2 = self.pedir(), self.pedir()
        self.assertEqual((e1["posicion_en_cola"], e2["posicion_en_cola"]), (1, 2))
        self.cli.post(f"/pedidos/1/cancelar")
        self.assertEqual(self.cli.get(f"/pedidos/{e1['id']}").json()["estado"], "ASIGNADO")
        self.assertEqual(self.cli.get(f"/pedidos/{e2['id']}").json()["estado"], "EN_COLA")

    def test_P5_pin_no_reclamado_en_24h_caduca_y_vuelve_a_inventario(self):
        pedido = self.pedir([{"sku": "PROTO-830", "cantidad": 3}])
        self.llevar_y_verificar(pedido["id"], 180.0)
        antes = self.stock("PROTO-830")
        self.assertEqual((antes["stock_disponible"], antes["stock_reservado"]), (12, 3))

        self.reloj.avanzar(hours=23, minutes=59)
        self.assertEqual(self.cli.post("/admin/caducar-vencidos").json()["pedidos_caducados"], [])
        self.reloj.avanzar(minutes=2)  # ya pasaron 24 h
        self.assertEqual(self.cli.post("/admin/caducar-vencidos").json()["pedidos_caducados"],
                         [pedido["id"]])

        self.assertEqual(self.cli.get(f"/pedidos/{pedido['id']}").json()["estado"], "CADUCADO")
        self.assertEqual(self.casillero(pedido["casillero"])["estado"], "LIBRE")
        despues = self.stock("PROTO-830")
        self.assertEqual((despues["stock_disponible"], despues["stock_reservado"]), (15, 0))
        # El PIN caducado ya no abre.
        r = self.cli.post(f"/casilleros/{pedido['casillero']}/abrir", json={"pin": self.pin_de(pedido["id"])})
        self.assertEqual(r.status_code, 403)

    def test_P5b_al_caducar_el_casillero_pasa_al_siguiente_en_cola(self):
        primero = self.pedir()
        for _ in range(5):
            self.pedir()
        espera = self.pedir()
        self.assertEqual(espera["estado"], "EN_COLA")
        self.llevar_y_verificar(primero["id"], 25.0)
        self.reloj.avanzar(hours=24, seconds=1)
        self.cli.post("/admin/caducar-vencidos")
        despues = self.cli.get(f"/pedidos/{espera['id']}").json()
        self.assertEqual((despues["estado"], despues["casillero"]), ("ASIGNADO", primero["casillero"]))

    def test_P6_peso_no_coincide_sin_pin_y_alerta_error_de_despacho(self):
        pedido = self.pedir([{"sku": "CAUTIN-30", "cantidad": 1}])
        v = self.llevar_y_verificar(pedido["id"], peso=95.0)  # esperado 130 g
        self.assertFalse(v["aprobado"])
        self.assertEqual(v["estado_pedido"], "RECHAZADO")
        notis = [n["mensaje"] for n in self.cli.get(f"/pedidos/{pedido['id']}/notificaciones").json()]
        self.assertFalse(any("PIN" in n and re.search(r"\d{6}", n) for n in notis))
        alertas = self.cli.get("/admin/alertas").json()
        self.assertIn("ERROR_DESPACHO", [a["tipo"] for a in alertas])
        self.assertEqual(self.casillero(pedido["casillero"])["estado"], "MANTENIMIENTO")
        self.assertEqual(self.stock("CAUTIN-30")["stock_reservado"], 0)
        r = self.cli.post(f"/casilleros/{pedido['casillero']}/abrir", json={"pin": "123456"})
        self.assertEqual(r.status_code, 403)

    def test_peso_en_el_limite_de_tolerancia(self):
        # Multímetro 350 g -> tolerancia max(2 g, 2 %) = 7 g.
        ok = self.pedir([{"sku": "MULTI-DT830", "cantidad": 1}])
        self.assertTrue(self.llevar_y_verificar(ok["id"], 357.0)["aprobado"])
        mal = self.pedir([{"sku": "MULTI-DT830", "cantidad": 1}])
        self.assertFalse(self.llevar_y_verificar(mal["id"], 357.5)["aprobado"])


class PruebasReglas(BaseApi):
    def test_sin_stock_rechaza_todo_el_carrito(self):
        r = self.pedir([{"sku": "ARD-UNO", "cantidad": 1}, {"sku": "CAUTIN-30", "cantidad": 99}],
                       esperado=409)
        self.assertEqual(r["error"], "SIN_STOCK")
        self.assertEqual(self.stock("ARD-UNO")["stock_reservado"], 0)  # no quedó nada a medias

    def test_producto_inexistente(self):
        self.assertEqual(self.pedir([{"sku": "NOPE", "cantidad": 1}], esperado=404)["error"],
                         "NO_ENCONTRADO")

    def test_cancelar_libera_stock_y_casillero(self):
        pedido = self.pedir()
        r = self.cli.post(f"/pedidos/{pedido['id']}/cancelar")
        self.assertEqual(r.json()["estado"], "CANCELADO")
        self.assertEqual(self.casillero(pedido["casillero"])["estado"], "LIBRE")
        self.assertEqual(self.stock("ARD-UNO")["stock_disponible"], 12)

    def test_no_se_cancela_cuando_ya_va_en_la_banda(self):
        pedido = self.pedir()
        self.cli.post(f"/maquina/pedidos/{pedido['id']}/llegada")
        self.assertEqual(self.cli.post(f"/pedidos/{pedido['id']}/cancelar").status_code, 409)

    def test_no_se_verifica_un_pedido_que_no_esta_en_el_casillero(self):
        pedido = self.pedir()
        r = self.cli.post(f"/maquina/pedidos/{pedido['id']}/verificacion",
                          json={"peso_medido_g": 25, "foto_ruta": "f.jpg"})
        self.assertEqual(r.status_code, 409)

    def test_foto_debe_ser_jpg(self):
        pedido = self.pedir()
        self.cli.post(f"/maquina/pedidos/{pedido['id']}/llegada")
        r = self.cli.post(f"/maquina/pedidos/{pedido['id']}/verificacion",
                          json={"peso_medido_g": 25, "foto_ruta": "foto.png"})
        self.assertEqual(r.status_code, 422)

    def test_el_pin_no_se_guarda_en_claro(self):
        pedido = self.pedir()
        self.llevar_y_verificar(pedido["id"], 25.0)
        pin = self.pin_de(pedido["id"])
        con = db.conectar(self.ruta)
        fila = con.execute("SELECT pin_hash, salt FROM pins").fetchone()
        con.close()
        self.assertNotIn(pin, fila["pin_hash"])
        self.assertEqual(len(fila["pin_hash"]), 64)

    def test_stock_bajo_genera_alerta_y_reabastecer_la_atiende(self):
        self.pedir([{"sku": "CAUTIN-30", "cantidad": 4}])  # 6 -> 2 = mínimo
        self.assertIn("STOCK_BAJO", [a["tipo"] for a in self.cli.get("/admin/alertas").json()])
        self.cli.post("/admin/productos/CAUTIN-30/reabastecer", json={"cantidad": 10})
        self.assertNotIn("STOCK_BAJO", [a["tipo"] for a in self.cli.get("/admin/alertas").json()])

    def test_mantenimiento_y_liberacion_reparte_la_cola(self):
        for c in ("A1", "A2", "A3", "A4", "B1", "B2"):
            self.cli.post(f"/admin/casilleros/{c}/mantenimiento")
        espera = self.pedir()
        self.assertEqual(espera["estado"], "EN_COLA")
        self.cli.post("/admin/casilleros/B2/liberar")
        self.assertEqual(self.cli.get(f"/pedidos/{espera['id']}").json()["estado"], "ASIGNADO")


class PruebasPantallas(BaseApi):
    def test_pantallas_cargan(self):
        for ruta in ("/", "/cliente", "/admin", "/pantallas/comun.js", "/pantallas/estilos.css"):
            self.assertEqual(self.cli.get(ruta).status_code, 200, ruta)

    def test_lista_de_pedidos_y_casilleros(self):
        a, b = self.pedir(), self.pedir()
        self.assertEqual([p["id"] for p in self.cli.get("/admin/pedidos").json()], [b["id"], a["id"]])
        self.assertEqual(self.cli.get("/casilleros").json(), ["A1", "A2", "A3", "A4", "B1", "B2"])

    def test_reloj_simulado_permite_probar_la_caducidad(self):
        pedido = self.pedir()
        self.llevar_y_verificar(pedido["id"], 25.0)
        r = self.cli.post("/admin/reloj/adelantar", json={"horas": 24})
        self.assertEqual(r.json()["adelanto_horas"], 24)
        self.assertEqual(self.cli.post("/admin/caducar-vencidos").json()["pedidos_caducados"],
                         [pedido["id"]])


if __name__ == "__main__":
    unittest.main()
