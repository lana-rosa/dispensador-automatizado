"""El administrador puede marcar un error de despacho como solucionado y continuar el proceso:
el pedido vuelve a VERIFICANDO para pesar de nuevo (el peso sigue decidiendo si hay PIN)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import BaseApi  # noqa: E402

# CAUTIN-30: 6 unidades de 130 g.


class PruebasResolverErrorDespacho(BaseApi):
    def pedido_rechazado(self, items=None):
        p = self.pedir(items or [{"sku": "CAUTIN-30", "cantidad": 1}])
        v = self.llevar_y_verificar(p["id"], 95.0)  # esperado 130 g
        self.assertEqual(v["estado_pedido"], "RECHAZADO")
        return p

    def resolver(self, pid, esperado=200):
        r = self.cli.post(f"/admin/pedidos/{pid}/resolver-despacho")
        self.assertEqual(r.status_code, esperado, r.text)
        return r.json()

    def alertas(self, tipo="ERROR_DESPACHO"):
        return [a for a in self.cli.get("/admin/alertas").json() if a["tipo"] == tipo]

    def test_resolver_devuelve_el_pedido_a_verificando(self):
        p = self.pedido_rechazado()
        self.assertEqual(self.casillero(p["casillero"])["estado"], "MANTENIMIENTO")
        self.assertEqual(self.stock("CAUTIN-30")["stock_reservado"], 0)
        r = self.resolver(p["id"])
        self.assertEqual(r["estado"], "VERIFICANDO")
        self.assertEqual(self.casillero(p["casillero"])["estado"], "RESERVADO")
        s = self.stock("CAUTIN-30")
        self.assertEqual((s["stock_disponible"], s["stock_reservado"]), (5, 1))
        self.assertEqual(self.alertas(), [])  # la alerta quedó atendida

    def test_despues_de_resolver_se_continua_hasta_entregar(self):
        p = self.pedido_rechazado()
        self.resolver(p["id"])
        r = self.cli.post(f"/maquina/pedidos/{p['id']}/verificacion",
                          json={"peso_medido_g": 130.0, "foto_ruta": "fotos/reintento.jpg"})
        self.assertTrue(r.json()["aprobado"])
        self.assertEqual(r.json()["estado_pedido"], "LISTO_RETIRO")
        ab = self.cli.post(f"/casilleros/{p['casillero']}/abrir", json={"pin": self.pin_de(p["id"])})
        self.assertTrue(ab.json()["abierto"])
        self.assertEqual(self.cli.get(f"/pedidos/{p['id']}").json()["estado"], "ENTREGADO")
        s = self.stock("CAUTIN-30")
        self.assertEqual((s["stock_disponible"], s["stock_reservado"]), (5, 0))

    def test_no_salta_la_verificacion_si_el_peso_sigue_mal(self):
        p = self.pedido_rechazado()
        self.resolver(p["id"])
        r = self.cli.post(f"/maquina/pedidos/{p['id']}/verificacion",
                          json={"peso_medido_g": 95.0, "foto_ruta": "fotos/otra.jpg"})
        self.assertFalse(r.json()["aprobado"])
        self.assertEqual(r.json()["estado_pedido"], "RECHAZADO")
        self.assertEqual(len(self.alertas()), 1)       # alerta nueva
        self.assertEqual(self.resolver(p["id"])["estado"], "VERIFICANDO")  # y se puede repetir

    def test_deja_rastro_en_la_bitacora_y_avisa_al_cliente(self):
        p = self.pedido_rechazado()
        self.resolver(p["id"])
        tipos = [b["tipo"] for b in self.cli.get(f"/pedidos/{p['id']}/trazabilidad").json()]
        self.assertIn("ERROR_DESPACHO_RESUELTO", tipos)
        mensajes = [n["mensaje"] for n in self.cli.get(f"/pedidos/{p['id']}/notificaciones").json()]
        self.assertTrue(any("Resolvimos el problema" in m for m in mensajes))

    def test_solo_se_resuelve_un_pedido_rechazado(self):
        p = self.pedir()
        self.assertEqual(self.resolver(p["id"], esperado=409)["error"], "ESTADO_INVALIDO")
        self.resolver(self.pedido_rechazado()["id"])  # y uno ya resuelto no se resuelve dos veces
        self.assertEqual(self.resolver(2, esperado=409)["error"], "ESTADO_INVALIDO")

    def test_si_el_casillero_ya_se_libero_no_se_puede_continuar(self):
        p = self.pedido_rechazado()
        self.cli.post(f"/admin/casilleros/{p['casillero']}/liberar")
        self.assertEqual(self.resolver(p["id"], esperado=409)["error"], "ESTADO_INVALIDO")
        self.assertEqual(self.cli.get(f"/pedidos/{p['id']}").json()["estado"], "RECHAZADO")

    def test_si_otro_pedido_se_llevo_el_stock_no_se_continua(self):
        p = self.pedido_rechazado()          # devolvió su unidad: hay 6
        self.pedir([{"sku": "CAUTIN-30", "cantidad": 6}])  # otro cliente se las lleva
        r = self.resolver(p["id"], esperado=409)
        self.assertEqual(r["error"], "SIN_STOCK")
        self.assertEqual(self.cli.get(f"/pedidos/{p['id']}").json()["estado"], "RECHAZADO")
        self.assertEqual(self.casillero(p["casillero"])["estado"], "MANTENIMIENTO")

    def test_un_cliente_no_puede_resolverlo(self):
        p = self.pedido_rechazado()
        r = self.anon.post(f"/admin/pedidos/{p['id']}/resolver-despacho",
                           headers={"Authorization": f"Bearer {self.token_cliente}"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
