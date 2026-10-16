"""Pruebas de cuentas y credenciales (Issue 1)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
from test_api import BaseApi  # noqa: E402


class PruebasCuentas(BaseApi):
    def registrar(self, correo="luis@x.co", usuario="luis", contrasena="clave-luis-123", esperado=201):
        r = self.anon.post("/cuentas/registro", json={"correo": correo, "usuario": usuario,
                                                      "contrasena": contrasena})
        self.assertEqual(r.status_code, esperado, r.text)
        return r.json()

    def como(self, token):
        return {"Authorization": f"Bearer {token}"}

    # ---------------------------------------------------------------- registro
    def test_registro_crea_cliente_e_inicia_sesion(self):
        r = self.registrar()
        self.assertEqual(r["usuario"]["rol"], "CLIENTE")
        yo = self.anon.get("/cuentas/yo", headers=self.como(r["token"])).json()
        self.assertEqual((yo["usuario"], yo["correo"]), ("luis", "luis@x.co"))

    def test_registro_valida_los_datos(self):
        self.assertEqual(self.registrar(correo="no-es-correo", esperado=422)["error"], "SOLICITUD_INVALIDA")
        self.registrar(usuario="a", esperado=422)
        self.registrar(contrasena="corta", esperado=422)

    def test_no_se_repiten_correo_ni_usuario(self):
        self.registrar()
        self.assertEqual(self.registrar(usuario="otro", esperado=409)["error"], "YA_EXISTE")
        self.registrar(correo="LUIS@X.CO", usuario="otro2", esperado=409)  # sin importar mayúsculas
        self.registrar(correo="otro@x.co", usuario="LUIS", esperado=409)

    def test_la_contrasena_no_se_guarda_en_claro(self):
        self.registrar(contrasena="mi-clave-secreta")
        con = db.conectar(self.ruta)
        fila = con.execute("SELECT password_hash, salt FROM usuarios WHERE usuario = 'luis'").fetchone()
        sesion = con.execute("SELECT token_hash FROM sesiones").fetchall()
        con.close()
        self.assertNotIn("mi-clave-secreta", fila["password_hash"])
        self.assertEqual(len(fila["password_hash"]), 64)
        self.assertTrue(all(len(s["token_hash"]) == 64 for s in sesion))

    # ---------------------------------------------------------------- login / logout
    def test_login_con_usuario_o_correo(self):
        self.registrar()
        for ident in ("luis", "luis@x.co"):
            r = self.anon.post("/cuentas/login", json={"usuario": ident, "contrasena": "clave-luis-123"})
            self.assertEqual(r.status_code, 200)

    def test_login_incorrecto_no_dice_que_fallo(self):
        self.registrar()
        a = self.anon.post("/cuentas/login", json={"usuario": "luis", "contrasena": "equivocada"})
        b = self.anon.post("/cuentas/login", json={"usuario": "nadie", "contrasena": "equivocada"})
        self.assertEqual((a.status_code, b.status_code), (401, 401))
        self.assertEqual(a.json()["detalle"], b.json()["detalle"])

    def test_logout_invalida_el_token(self):
        token = self.registrar()["token"]
        self.anon.post("/cuentas/logout", headers=self.como(token))
        self.assertEqual(self.anon.get("/cuentas/yo", headers=self.como(token)).status_code, 401)

    def test_la_sesion_vence(self):
        con = db.conectar(self.ruta)
        con.execute("UPDATE configuracion SET valor = '12' WHERE clave = 'horas_sesion'")
        con.close()
        token = self.registrar()["token"]
        self.reloj.avanzar(hours=12, seconds=1)
        self.assertEqual(self.anon.get("/cuentas/yo", headers=self.como(token)).status_code, 401)

    # ---------------------------------------------------------------- permisos
    def test_sin_sesion_no_se_puede_pedir(self):
        r = self.anon.post("/pedidos", json={"items": [{"sku": "ARD-UNO", "cantidad": 1}]})
        self.assertEqual(r.status_code, 401)

    def test_el_catalogo_y_el_teclado_del_casillero_no_piden_sesion(self):
        self.assertEqual(self.anon.get("/productos").status_code, 200)
        r = self.anon.post("/casilleros/A1/abrir", json={"pin": "123456"})
        self.assertEqual(r.json()["motivo"], "SIN_PIN_VIGENTE")

    def test_rutas_de_admin_y_maquina_exigen_administrador(self):
        for ruta in ("/admin/inventario", "/admin/alertas", "/admin/maquina"):
            self.assertEqual(self.anon.get(ruta).status_code, 401, ruta)
            self.assertEqual(self.anon.get(ruta, headers=self.como(self.token_cliente)).status_code, 403, ruta)
            self.assertEqual(self.anon.get(ruta, headers=self.como(self.token_admin)).status_code, 200, ruta)
        pedido = self.pedir()
        r = self.anon.post(f"/maquina/pedidos/{pedido['id']}/despachar", headers=self.como(self.token_cliente))
        self.assertEqual(r.status_code, 403)

    def test_no_hay_registro_publico_de_administradores(self):
        r = self.anon.post("/cuentas/registro", json={"correo": "x@x.co", "usuario": "hacker",
                                                      "contrasena": "clave-123456", "rol": "ADMIN"})
        self.assertEqual(r.json()["usuario"]["rol"], "CLIENTE")

    def test_el_pedido_queda_a_nombre_de_la_cuenta(self):
        pedido = self.pedir()
        self.assertEqual(pedido["cliente_nombre"], "ana")
        self.assertEqual([p["id"] for p in self.cli.get("/mis-pedidos").json()], [pedido["id"]])

    def test_un_cliente_no_ve_pedidos_ajenos(self):
        pedido = self.pedir()
        otro = self.registrar()["token"]
        for ruta in (f"/pedidos/{pedido['id']}", f"/pedidos/{pedido['id']}/notificaciones"):
            self.assertEqual(self.anon.get(ruta, headers=self.como(otro)).status_code, 404, ruta)
        self.assertEqual(self.anon.post(f"/pedidos/{pedido['id']}/cancelar", headers=self.como(otro)).status_code, 404)
        self.assertEqual(self.anon.get("/mis-pedidos", headers=self.como(otro)).json(), [])
        # El administrador sí lo ve.
        self.assertEqual(self.anon.get(f"/pedidos/{pedido['id']}", headers=self.como(self.token_admin)).status_code, 200)

    def test_el_administrador_no_hace_pedidos_de_cliente(self):
        r = self.anon.post("/pedidos", json={"items": [{"sku": "ARD-UNO", "cantidad": 1}]},
                           headers=self.como(self.token_admin))
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
