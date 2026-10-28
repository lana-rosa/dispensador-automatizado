"""Issue #4: validación de los datos de entrada y alerta cuando falla la báscula."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import BaseApi  # noqa: E402


class PruebasValidacionEntradas(BaseApi):
    def test_cantidad_fuera_de_rango_da_422(self):
        for cantidad in (0, -1, 101):
            self.pedir([{"sku": "ARD-UNO", "cantidad": cantidad}], esperado=422)

    def test_sku_vacio_o_demasiado_largo_da_422(self):
        self.pedir([{"sku": "", "cantidad": 1}], esperado=422)
        self.pedir([{"sku": "X" * 41, "cantidad": 1}], esperado=422)

    def test_carrito_vacio_o_con_mas_de_30_lineas_da_422(self):
        self.assertEqual(self.cli.post("/pedidos", json={"items": []}).status_code, 422)
        self.pedir([{"sku": f"S{i}", "cantidad": 1} for i in range(31)], esperado=422)

    def test_cantidad_que_no_es_numero_da_422(self):
        self.pedir([{"sku": "ARD-UNO", "cantidad": "dos"}], esperado=422)

    def test_reabastecer_con_cantidad_absurda_da_422(self):
        r = self.cli.post("/admin/productos/ARD-UNO/reabastecer", json={"cantidad": 100_001})
        self.assertEqual(r.status_code, 422)


class PruebasFallaBascula(BaseApi):
    def pedido_en_verificacion(self):
        p = self.pedir([{"sku": "ARD-UNO", "cantidad": 2}])
        self.assertEqual(self.cli.post(f"/maquina/pedidos/{p['id']}/despachar").status_code, 200)
        return p["id"]

    def verificar(self, pid, cuerpo):
        return self.cli.post(f"/maquina/pedidos/{pid}/verificacion", content=cuerpo,
                             headers={"Content-Type": "application/json"})

    def alertas_bascula(self):
        return [a for a in self.cli.get("/admin/alertas").json() if a["tipo"] == "BASCULA"]

    def test_lecturas_imposibles_dan_422_y_alertan(self):
        malas = {
            "sin peso": '{"foto_ruta": "fotos/p.jpg"}',
            "peso nulo": '{"peso_medido_g": null, "foto_ruta": "fotos/p.jpg"}',
            "peso negativo": '{"peso_medido_g": -5, "foto_ruta": "fotos/p.jpg"}',
            "NaN": '{"peso_medido_g": NaN, "foto_ruta": "fotos/p.jpg"}',
            "enorme": '{"peso_medido_g": 1e9, "foto_ruta": "fotos/p.jpg"}',
        }
        for nombre, cuerpo in malas.items():
            with self.subTest(nombre):
                pid = self.pedido_en_verificacion()
                r = self.verificar(pid, cuerpo)
                self.assertEqual(r.status_code, 422, r.text)
                self.assertEqual(r.json()["error"], "SOLICITUD_INVALIDA")
                self.assertTrue(any(a["pedido_id"] == pid for a in self.alertas_bascula()))
                # El pedido no cambió: sigue esperando una lectura válida.
                self.assertEqual(self.cli.get(f"/pedidos/{pid}").json()["estado"], "VERIFICANDO")
                bit = [b["tipo"] for b in self.cli.get(f"/pedidos/{pid}/trazabilidad").json()]
                self.assertIn("BASCULA_FALLA", bit)

    def test_se_puede_reintentar_con_una_lectura_valida(self):
        pid = self.pedido_en_verificacion()
        self.assertEqual(self.verificar(pid, '{"peso_medido_g": -1, "foto_ruta": "fotos/p.jpg"}'
                                        ).status_code, 422)
        r = self.verificar(pid, '{"peso_medido_g": 50, "foto_ruta": "fotos/p.jpg"}')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["aprobado"])
        self.assertEqual(r.json()["estado_pedido"], "LISTO_RETIRO")

    def test_no_se_repite_la_alerta_del_mismo_pedido(self):
        pid = self.pedido_en_verificacion()
        for _ in range(3):
            self.verificar(pid, '{"peso_medido_g": -1, "foto_ruta": "fotos/p.jpg"}')
        self.assertEqual(len(self.alertas_bascula()), 1)

    def test_un_peso_valido_pero_distinto_no_es_falla_de_bascula(self):
        pid = self.pedido_en_verificacion()
        r = self.verificar(pid, '{"peso_medido_g": 95, "foto_ruta": "fotos/p.jpg"}')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["aprobado"])  # P6: error de despacho, no falla de báscula
        self.assertEqual(self.alertas_bascula(), [])

    def test_la_foto_sigue_siendo_obligatoria(self):
        pid = self.pedido_en_verificacion()
        r = self.verificar(pid, '{"peso_medido_g": 50}')
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
