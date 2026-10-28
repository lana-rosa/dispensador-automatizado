"""Inventario: stock disponible y reservado, y reabastecimiento."""

from .nucleo import NoEncontrado, SolicitudInvalida, ahora_utc, fmt, transaccion, _bitacora


def _liberar_stock(con, pedido_id, devolver: bool):
    """Quita lo reservado. devolver=True lo regresa a disponible (cancelado/caducado);
    devolver=False lo da por consumido (entregado)."""
    for it in con.execute("SELECT producto_id, cantidad FROM pedido_items WHERE pedido_id = ?",
                          (pedido_id,)):
        con.execute(
            "UPDATE productos SET stock_reservado = stock_reservado - ?,"
            " stock_disponible = stock_disponible + ? WHERE id = ?",
            (it["cantidad"], it["cantidad"] if devolver else 0, it["producto_id"]),
        )


def inventario(con):
    return [dict(f) for f in con.execute(
        "SELECT sku, nombre, categoria, peso_unitario_g, stock_disponible, stock_reservado,"
        " stock_minimo, (stock_disponible <= stock_minimo) AS stock_bajo"
        " FROM productos WHERE activo = 1 ORDER BY sku")]


def reabastecer(con, sku, cantidad, ahora=None):
    ahora = ahora or ahora_utc()
    if cantidad <= 0:
        raise SolicitudInvalida("La cantidad debe ser positiva.")
    with transaccion(con):
        prod = con.execute("SELECT * FROM productos WHERE sku = ?", (sku,)).fetchone()
        if prod is None:
            raise NoEncontrado(f"No existe el producto {sku}.")
        con.execute("UPDATE productos SET stock_disponible = stock_disponible + ? WHERE id = ?",
                    (cantidad, prod["id"]))
        _bitacora(con, ahora, "REABASTECIMIENTO", f"{sku} +{cantidad}")
        if prod["stock_disponible"] + cantidad > prod["stock_minimo"]:
            con.execute("UPDATE alertas SET atendida = 1, atendida_en = ? WHERE tipo = 'STOCK_BAJO'"
                        " AND atendida = 0 AND producto_id = ?", (fmt(ahora), prod["id"]))
    return inventario(con)
