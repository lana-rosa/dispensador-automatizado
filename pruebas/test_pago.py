"""RF-05: el inventario se descuenta al PAGAR (pago simulado) y, si falta stock de algún producto,
el pedido sigue con lo disponible en vez de rechazarse completo."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import BaseApi  # noqa: E402

# Inventario de demostración: ARD-UNO 12 u. (25 g), CAUTIN-30 6 u. (130 g), mínimo 2.


class PruebasPago(BaseApi):
    def test_crear_el_pedido_no_descuenta_inventario_ni_asigna_casillero(self):
        antes = self.stock("ARD-UNO")
        p = self.pedir([{"sku": "ARD-UNO", "cantidad": 3}], pagar=False)
        self.assertEqual(p["estado"], "PENDIENTE_PAGO")
        self.assertIsNone(p["casillero"])
        despues = self.stock("ARD-UNO")
        self.assertEqual(despues["stock_disponible"], antes["stock_disponible"])
        self.assertEqual(despues["stock_reservado"], 0)

    def test_pagar_descuenta_el_inventario_y_reserva_casillero(self):
        p = self.pedir([{"sku": "ARD-UNO", "cantidad": 3}], pagar=False)
        pagado = self.pagar(p["id"])
        self.assertTrue(pagado["pago_aprobado"])
        self.assertEqual(pagado["estado"], "ASIGNADO")
        self.assertEqual(pagado["casillero"], "A1")
        self.assertEqual(pagado["faltantes"], [])
        s = self.stock("ARD-UNO")
        self.assertEqual((s["stock_disponible"], s["stock_reservado"]), (9, 3))

    def test_pago_rechazado_no_toca_el_inventario_y_se_puede_reintentar(self):
        p = self.pedir([{"sku": "ARD-UNO", "cantidad": 3}], pagar=False)
        r = self.pagar(p["id"], esperado=402, resultado="RECHAZADO")
        self.assertFalse(r["pago_aprobado"])
        self.assertEqual(r["estado"], "PENDIENTE_PAGO")
        self.assertEqual(self.stock("ARD-UNO")["stock_reservado"], 0)
        self.assertEqual(self.pagar(p["id"])["estado"], "ASIGNADO")

    def test_no_se_paga_dos_veces(self):
        p = self.pedir([{"sku": "ARD-UNO", "cantidad": 1}])
        self.pagar(p["id"], esperado=409)
        self.assertEqual(self.stock("ARD-UNO")["stock_reservado"], 1)

    def test_si_falta_stock_el_pedido_sigue_con_lo_disponible(self):
        p = self.pedir([{"sku": "ARD-UNO", "cantidad": 1}, {"sku": "CAUTIN-30", "cantidad": 10}],
                       pagar=False)
        pagado = self.pagar(p["id"])
        self.assertEqual(pagado["estado"], "ASIGNADO")
        self.assertEqual(pagado["faltantes"],
                         [{"sku": "CAUTIN-30", "nombre": "Cautín 30W", "pedidas": 10,
                           "confirmadas": 6}])
        cautin = self.stock("CAUTIN-30")
        self.assertEqual((cautin["stock_disponible"], cautin["stock_reservado"]), (0, 6))
        self.assertEqual(pagado["peso_teorico_g"], 25 * 1 + 130 * 6)
        mensajes = [n["mensaje"] for n in self.cli.get(f"/pedidos/{p['id']}/notificaciones").json()]
        self.assertTrue(any("CAUTIN-30" in m and "pediste 10" in m for m in mensajes))

    def test_el_pedido_parcial_se_completa_con_el_peso_confirmado(self):
        p = self.pedir([{"sku": "ARD-UNO", "cantidad": 1}, {"sku": "CAUTIN-30", "cantidad": 10}],
                       pagar=False)
        pagado = self.pagar(p["id"])
        r = self.llevar_y_verificar(p["id"], pagado["peso_teorico_g"])
        self.assertTrue(r["aprobado"])
        self.assertEqual(r["estado_pedido"], "LISTO_RETIRO")
        ab = self.cli.post(f"/casilleros/{pagado['casillero']}/abrir", json={"pin": self.pin_de(p["id"])})
        self.assertTrue(ab.json()["abierto"])
        s = self.stock("CAUTIN-30")
        self.assertEqual((s["stock_disponible"], s["stock_reservado"]), (0, 0))  # consumido

    def test_sin_unidades_de_ningun_producto_no_se_cobra(self):
        self.pedir([{"sku": "CAUTIN-30", "cantidad": 6}])  # agota el producto
        p = self.pedir([{"sku": "CAUTIN-30", "cantidad": 2}], pagar=False)
        r = self.pagar(p["id"], esperado=409)
        self.assertEqual(r["error"], "SIN_STOCK")
        self.assertEqual(self.cli.get(f"/pedidos/{p['id']}").json()["estado"], "PENDIENTE_PAGO")
        self.cli.post("/admin/productos/CAUTIN-30/reabastecer", json={"cantidad": 5})
        self.assertEqual(self.pagar(p["id"])["estado"], "ASIGNADO")

    def test_el_stock_es_en_tiempo_real_entre_pedidos_pendientes(self):
        a = self.pedir([{"sku": "ARD-UNO", "cantidad": 8}], pagar=False)
        b = self.pedir([{"sku": "ARD-UNO", "cantidad": 8}], pagar=False)
        self.assertEqual(self.pagar(a["id"])["faltantes"], [])
        segundo = self.pagar(b["id"])  # solo quedaban 4
        self.assertEqual(segundo["faltantes"][0]["confirmadas"], 4)
        s = self.stock("ARD-UNO")
        self.assertEqual((s["stock_disponible"], s["stock_reservado"]), (0, 12))

    def test_cancelar_sin_pagar_no_toca_el_inventario(self):
        p = self.pedir([{"sku": "ARD-UNO", "cantidad": 3}], pagar=False)
        self.assertEqual(self.cli.post(f"/pedidos/{p['id']}/cancelar").json()["estado"], "CANCELADO")
        s = self.stock("ARD-UNO")
        self.assertEqual((s["stock_disponible"], s["stock_reservado"]), (12, 0))

    def test_cancelar_despues_de_pagar_devuelve_el_inventario(self):
        p = self.pedir([{"sku": "ARD-UNO", "cantidad": 3}])
        self.cli.post(f"/pedidos/{p['id']}/cancelar")
        s = self.stock("ARD-UNO")
        self.assertEqual((s["stock_disponible"], s["stock_reservado"]), (12, 0))

    def test_la_cola_funciona_despues_del_pago(self):
        for _ in range(6):
            self.pedir()
        extra = self.pedir()
        self.assertEqual(extra["estado"], "EN_COLA")
        self.assertEqual(extra["posicion_en_cola"], 1)

    def test_stock_bajo_se_alerta_al_pagar_no_al_crear(self):
        p = self.pedir([{"sku": "CAUTIN-30", "cantidad": 4}], pagar=False)  # 6 -> 2 = mínimo
        self.assertNotIn("STOCK_BAJO", [a["tipo"] for a in self.cli.get("/admin/alertas").json()])
        self.pagar(p["id"])
        self.assertIn("STOCK_BAJO", [a["tipo"] for a in self.cli.get("/admin/alertas").json()])

    def test_un_cliente_no_paga_el_pedido_de_otro(self):
        p = self.pedir([{"sku": "ARD-UNO", "cantidad": 1}], pagar=False)
        otro = self.anon.post("/cuentas/registro", json={
            "correo": "luis@x.co", "usuario": "luis", "contrasena": "clave-luis-123"}).json()["token"]
        r = self.anon.post(f"/pedidos/{p['id']}/pagar", headers={"Authorization": f"Bearer {otro}"})
        self.assertEqual(r.status_code, 404)

    def test_pagar_sin_sesion_da_401(self):
        p = self.pedir(pagar=False)
        self.assertEqual(self.anon.post(f"/pedidos/{p['id']}/pagar").status_code, 401)


if __name__ == "__main__":
    unittest.main()
