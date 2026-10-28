"""Alertas del administrador: listar, atender y cerrar las que ya no aplican."""

from .nucleo import NoEncontrado, ahora_utc, fmt, transaccion


def _atender_alertas(con, ahora, tipo, pedido_id):
    con.execute("UPDATE alertas SET atendida = 1, atendida_en = ? WHERE tipo = ?"
                " AND atendida = 0 AND pedido_id = ?", (fmt(ahora), tipo, pedido_id))


def listar_alertas(con, solo_pendientes=True):
    sql = "SELECT * FROM alertas" + (" WHERE atendida = 0" if solo_pendientes else "")
    return [dict(f) for f in con.execute(sql + " ORDER BY id DESC")]


def atender_alerta(con, alerta_id, ahora=None):
    ahora = ahora or ahora_utc()
    with transaccion(con):
        if con.execute("SELECT 1 FROM alertas WHERE id = ?", (alerta_id,)).fetchone() is None:
            raise NoEncontrado(f"No existe la alerta {alerta_id}.")
        con.execute("UPDATE alertas SET atendida = 1, atendida_en = ? WHERE id = ?",
                    (fmt(ahora), alerta_id))
