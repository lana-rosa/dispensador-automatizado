"""Pruebas de la máquina de estados (Issue 2): banda, sensores, actuador, cerraduras y P4."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import maquina  # noqa: E402
from test_api import BaseApi  # noqa: E402


def detalles(eventos, tipo):
    return [e.detalle for e in eventos if e.tipo == tipo]


class PruebasMaquinaDeEstados(unittest.TestCase):
    """La lógica de control sola, sin base de datos."""

    def test_recorrido_normal_pasa_por_todos_los_estados(self):
        r = maquina.simular_recorrido("A1", timeout_s=10)
        self.assertTrue(r.exito)
        self.assertEqual(r.estado_final, maquina.EstadoBanda.ENTREGADO)
        self.assertEqual(detalles(r.eventos, "ESTADO"), [
            "REPOSO -> ESPERANDO_ENTRADA", "ESPERANDO_ENTRADA -> TRANSPORTANDO",
            "TRANSPORTANDO -> DESVIANDO", "DESVIANDO -> ENTREGADO"])
        self.assertEqual(detalles(r.eventos, "SENSOR"), ["S_ENTRADA=1", "S_DESVIO=1", "S_CASILLERO=1"])
        self.assertEqual(detalles(r.eventos, "SENAL"), [
            "MOTOR_BANDA=ON", "ACTUADOR_DESVIO=ON (casillero A1)",
            "ACTUADOR_DESVIO=OFF", "MOTOR_BANDA=OFF"])
        self.assertEqual(r.duracion_s, 6.5)

    def test_atasco_detiene_motor_tras_el_tiempo_maximo(self):
        r = maquina.simular_recorrido("A1", timeout_s=10, atasco_en="S_DESVIO")
        self.assertFalse(r.exito)
        self.assertEqual(r.estado_final, maquina.EstadoBanda.DETENIDA_ATASCO)
        self.assertEqual(r.sensor_fallido, "S_DESVIO")
        self.assertEqual(r.duracion_s, 11.0)  # 1 s hasta S_ENTRADA + 10 s esperando S_DESVIO
        self.assertEqual(detalles(r.eventos, "SENAL")[-1], "MOTOR_BANDA=OFF")
        self.assertNotIn("S_CASILLERO=1", detalles(r.eventos, "SENSOR"))

    def test_atasco_durante_el_desvio_apaga_tambien_el_actuador(self):
        r = maquina.simular_recorrido("B2", timeout_s=10, atasco_en="S_CASILLERO")
        self.assertEqual(detalles(r.eventos, "SENAL")[-2:], ["MOTOR_BANDA=OFF", "ACTUADOR_DESVIO=OFF"])

    def test_caja_demasiado_lenta_cuenta_como_atasco(self):
        r = maquina.simular_recorrido("A1", timeout_s=10, tiempos_s={"S_DESVIO": 12})
        self.assertFalse(r.exito)
        self.assertEqual(r.sensor_fallido, "S_DESVIO")

    def test_sensor_desconocido(self):
        with self.assertRaises(ValueError):
            maquina.simular_recorrido("A1", timeout_s=10, atasco_en="S_INVENTADO")

    def test_ciclo_de_electrocerradura(self):
        ev = maquina.ciclo_cerradura("A3")
        self.assertEqual(detalles(ev, "SENAL"), ["ELECTROCERRADURA_A3=ON", "ELECTROCERRADURA_A3=OFF"])
        self.assertEqual(detalles(ev, "ESTADO"), ["BLOQUEADA -> ABIERTA", "ABIERTA -> BLOQUEADA"])


class PruebasBandaEnElSistema(BaseApi):
    """La máquina de estados conectada a pedidos, alertas y pantallas."""

    def despachar(self, pedido_id, atasco_en=None, esperado=200):
        cuerpo = {"atasco_en": atasco_en} if atasco_en else None
        r = self.cli.post(f"/maquina/pedidos/{pedido_id}/despachar", json=cuerpo)
        self.assertEqual(r.status_code, esperado, r.text)
        return r.json()

    def tipos_bitacora(self, pedido_id):
        return [(e["tipo"], e["detalle"]) for e in self.cli.get(f"/pedidos/{pedido_id}/trazabilidad").json()]

    def test_despacho_normal_deja_el_pedido_verificando(self):
        pedido = self.pedir()
        r = self.despachar(pedido["id"])
        self.assertTrue(r["exito"])
        self.assertEqual(r["pedido"]["estado"], "VERIFICANDO")
        traza = self.tipos_bitacora(pedido["id"])
        self.assertIn(("BANDA_SENSOR", "t=6.5s S_CASILLERO=1"), traza)
        self.assertIn(("BANDA_SENAL", "t=6.5s MOTOR_BANDA=OFF"), traza)

    def test_P4_atasco_detiene_la_banda_y_alerta(self):
        pedido = self.pedir()
        r = self.despachar(pedido["id"], atasco_en="S_DESVIO")
        self.assertFalse(r["exito"])
        self.assertEqual(r["pedido"]["estado"], "ATASCADO")
        self.assertEqual(r["sensor_fallido"], "S_DESVIO")
        self.assertEqual(r["eventos"][-2]["detalle"], "MOTOR_BANDA=OFF")
        alertas = self.cli.get("/admin/alertas").json()
        self.assertEqual([a["tipo"] for a in alertas], ["ATASCO"])
        self.assertIn("S_DESVIO", alertas[0]["mensaje"])
        maq = self.cli.get("/admin/maquina").json()
        self.assertEqual((maq["banda"], maq["pedido_atascado"]), ("DETENIDA_ATASCO", pedido["id"]))
        self.assertEqual(self.casillero(pedido["casillero"])["estado"], "RESERVADO")
        # Sin llegar al casillero no hay verificación ni PIN.
        v = self.cli.post(f"/maquina/pedidos/{pedido['id']}/verificacion",
                          json={"peso_medido_g": 25, "foto_ruta": "f.jpg"})
        self.assertEqual(v.status_code, 409)

    def test_banda_detenida_no_despacha_otros_pedidos(self):
        a, b = self.pedir(), self.pedir()
        self.despachar(a["id"], atasco_en="S_ENTRADA")
        r = self.despachar(b["id"], esperado=409)
        self.assertIn("detenida", r["detalle"])

    def test_reintento_tras_atasco_completa_el_flujo(self):
        pedido = self.pedir()
        self.despachar(pedido["id"], atasco_en="S_CASILLERO")
        r = self.despachar(pedido["id"])  # el operario despejó la banda
        self.assertTrue(r["exito"])
        self.assertEqual(self.cli.get("/admin/alertas").json(), [])
        self.assertEqual(self.cli.get("/admin/maquina").json()["banda"], "REPOSO")
        v = self.cli.post(f"/maquina/pedidos/{pedido['id']}/verificacion",
                          json={"peso_medido_g": 25, "foto_ruta": "f.jpg"}).json()
        self.assertTrue(v["aprobado"])
        abrir = self.cli.post(f"/casilleros/{pedido['casillero']}/abrir", json={"pin": self.pin_de(pedido["id"])})
        self.assertEqual(abrir.status_code, 200)

    def test_admin_cancela_pedido_atascado(self):
        pedido = self.pedir([{"sku": "CAUTIN-30", "cantidad": 2}])
        self.despachar(pedido["id"], atasco_en="S_DESVIO")
        self.assertEqual(self.cli.post(f"/pedidos/{pedido['id']}/cancelar").status_code, 409)  # cliente no
        r = self.cli.post(f"/admin/pedidos/{pedido['id']}/cancelar")
        self.assertEqual(r.json()["estado"], "CANCELADO")
        self.assertEqual(self.casillero(pedido["casillero"])["estado"], "LIBRE")
        self.assertEqual(self.stock("CAUTIN-30")["stock_disponible"], 6)
        self.assertEqual(self.cli.get("/admin/maquina").json()["banda"], "REPOSO")
        self.assertEqual(self.cli.get("/admin/alertas").json(), [])

    def test_apertura_registra_el_ciclo_de_la_electrocerradura(self):
        pedido = self.pedir()
        self.llevar_y_verificar(pedido["id"], 25.0)
        self.cli.post(f"/casilleros/{pedido['casillero']}/abrir", json={"pin": self.pin_de(pedido["id"])})
        senales = [d for t, d in self.tipos_bitacora(pedido["id"]) if t == "CERRADURA_SENAL"]
        c = pedido["casillero"]
        self.assertEqual(senales, [f"t=0s ELECTROCERRADURA_{c}=ON", f"t=8s ELECTROCERRADURA_{c}=OFF"])

    def test_pin_incorrecto_no_activa_la_electrocerradura(self):
        pedido = self.pedir()
        self.llevar_y_verificar(pedido["id"], 25.0)
        self.cli.post(f"/casilleros/{pedido['casillero']}/abrir", json={"pin": "999999"})
        self.assertNotIn("CERRADURA_SENAL", [t for t, _ in self.tipos_bitacora(pedido["id"])])

    def test_sensor_de_atasco_invalido(self):
        pedido = self.pedir()
        self.despachar(pedido["id"], atasco_en="S_INVENTADO", esperado=422)


if __name__ == "__main__":
    unittest.main()
