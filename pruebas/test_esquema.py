"""Pruebas del esquema: las reglas de integridad viven en la propia BD."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db  # noqa: E402


class PruebasEsquema(unittest.TestCase):
    def setUp(self):
        self.con = db.inicializar(":memory:")

    def tearDown(self):
        self.con.close()

    def _pedido(self, estado="EN_COLA", casillero_id=None, producto_id=1):
        return self.con.execute(
            "INSERT INTO pedidos (cliente_nombre, cliente_contacto, producto_id, cantidad,"
            " peso_teorico_g, estado, casillero_id) VALUES ('Ana', 'ana@mail.co', ?, 1, 25, ?, ?)",
            (producto_id, estado, casillero_id),
        ).lastrowid

    def test_datos_demo_cargados(self):
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM productos").fetchone()[0], 6)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM casilleros").fetchone()[0], 6)
        tol = self.con.execute(
            "SELECT valor FROM configuracion WHERE clave = 'horas_reserva'"
        ).fetchone()[0]
        self.assertEqual(tol, "24")

    def test_stock_no_puede_ser_negativo(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE productos SET stock_disponible = -1 WHERE id = 1")

    def test_estado_de_pedido_invalido(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._pedido(estado="VOLANDO")

    def test_pedido_en_cola_no_puede_tener_casillero(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._pedido(estado="EN_COLA", casillero_id=1)

    def test_pedido_en_proceso_requiere_casillero(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._pedido(estado="ASIGNADO", casillero_id=None)

    def test_un_casillero_no_admite_dos_pedidos_activos(self):
        self._pedido(estado="ASIGNADO", casillero_id=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self._pedido(estado="ASIGNADO", casillero_id=1)

    def test_casillero_se_reutiliza_cuando_el_pedido_termina(self):
        self._pedido(estado="ENTREGADO", casillero_id=1)
        self._pedido(estado="ASIGNADO", casillero_id=1)  # no debe fallar

    def test_un_solo_pin_vigente_por_casillero(self):
        p1 = self._pedido(estado="ASIGNADO", casillero_id=1)
        sql = ("INSERT INTO pins (pedido_id, casillero_id, pin_hash, salt, estado, expira_en)"
               " VALUES (?, 1, 'h', 's', ?, '2026-10-09T00:00:00Z')")
        self.con.execute(sql, (p1, "VIGENTE"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(sql, (p1, "VIGENTE"))
        self.con.execute(sql, (p1, "CADUCADO"))  # los históricos sí pueden repetirse

    def test_claves_foraneas_activas(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._pedido(producto_id=999)

    def test_tipo_de_alerta_invalido(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO alertas (tipo, mensaje) VALUES ('OTRA', 'x')")


if __name__ == "__main__":
    unittest.main()
