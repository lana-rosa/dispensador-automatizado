"""Pedidos: crear el carrito, pagar (simulado, descuenta stock), consultar y cancelar."""

from .nucleo import (
    NoEncontrado, SinStock, EstadoInvalido, SolicitudInvalida, ahora_utc, fmt,
    transaccion, _bitacora, _alerta, _notificar, _pedido_fila, _set_casillero,
    _cambiar_estado,
)
from .alertas import _atender_alertas
from .inventario import _liberar_stock
from .casilleros import _asignar, _despachar_cola


def crear_pedido(con, cliente_nombre, cliente_contacto, items, ahora=None, usuario_id=None):
    """items: lista de (sku, cantidad). Guarda el carrito en estado PENDIENTE_PAGO: todavía NO
    se descuenta inventario ni se asigna casillero (eso ocurre al pagar, ver pagar_pedido)."""
    ahora = ahora or ahora_utc()
    if not items:
        raise SolicitudInvalida("El carrito está vacío.")
    carrito: dict[str, int] = {}  # si repiten un sku, se suman
    for sku, cantidad in items:
        if cantidad <= 0:
            raise SolicitudInvalida(f"Cantidad inválida para {sku}.")
        carrito[sku] = carrito.get(sku, 0) + cantidad

    with transaccion(con):
        lineas = []
        for sku, cantidad in carrito.items():
            prod = con.execute("SELECT * FROM productos WHERE sku = ? AND activo = 1",
                               (sku,)).fetchone()
            if prod is None:
                raise NoEncontrado(f"No existe el producto {sku}.")
            lineas.append((prod, cantidad))

        peso_teorico = sum(p["peso_unitario_g"] * c for p, c in lineas)
        pedido_id = con.execute(
            "INSERT INTO pedidos (usuario_id, cliente_nombre, cliente_contacto, peso_teorico_g,"
            " creado_en, actualizado_en) VALUES (?, ?, ?, ?, ?, ?)",
            (usuario_id, cliente_nombre, cliente_contacto, peso_teorico, fmt(ahora), fmt(ahora)),
        ).lastrowid
        for prod, cantidad in lineas:
            con.execute(
                "INSERT INTO pedido_items (pedido_id, producto_id, cantidad, peso_unitario_g,"
                " cantidad_pedida) VALUES (?, ?, ?, ?, ?)",
                (pedido_id, prod["id"], cantidad, prod["peso_unitario_g"], cantidad),
            )
        _bitacora(con, ahora, "PEDIDO_CREADO",
                  f"{len(lineas)} producto(s), peso teórico {peso_teorico:g} g. Pendiente de pago.",
                  pedido_id)
        _notificar(con, ahora, pedido_id,
                   f"Tu pedido #{pedido_id} está pendiente de pago. Al pagar se descuentan las "
                   "unidades del inventario y se reserva tu casillero.")
    return obtener_pedido(con, pedido_id)


def pagar_pedido(con, pedido_id, aprobado=True, ahora=None):
    """Pago SIMULADO (no hay pasarela real). Al aprobarse:
      * se descuenta el inventario en tiempo real: de cada producto se toma lo que pidió el
        cliente o, si ya quedan menos unidades, lo que haya (el pedido NO se rechaza entero);
      * si no hay unidades de NINGÚN producto, se lanza SinStock y no se cobra nada;
      * el peso teórico se recalcula con lo confirmado y el pedido recibe casillero o pasa a la
        cola. El cliente ve en el pedido qué faltó (cantidad_pedida vs cantidad).
    Un pago rechazado deja el pedido PENDIENTE_PAGO para reintentar."""
    ahora = ahora or ahora_utc()
    with transaccion(con):
        pedido = _pedido_fila(con, pedido_id)
        if pedido["estado"] != "PENDIENTE_PAGO":
            raise EstadoInvalido(
                f"El pedido {pedido_id} está {pedido['estado']}; solo se paga en PENDIENTE_PAGO.")
        if not aprobado:
            _bitacora(con, ahora, "PAGO_RECHAZADO", "Pago simulado rechazado.", pedido_id)
            _notificar(con, ahora, pedido_id,
                       f"El pago del pedido #{pedido_id} fue rechazado. Puedes intentarlo de nuevo.")
            return {**obtener_pedido(con, pedido_id), "pago_aprobado": False}

        lineas = con.execute(
            "SELECT i.producto_id, i.cantidad_pedida, i.peso_unitario_g, pr.sku, pr.stock_disponible,"
            " pr.stock_minimo FROM pedido_items i JOIN productos pr ON pr.id = i.producto_id"
            " WHERE i.pedido_id = ? ORDER BY pr.sku", (pedido_id,)).fetchall()
        confirmadas = [(l, min(l["cantidad_pedida"], l["stock_disponible"])) for l in lineas]
        if sum(c for _, c in confirmadas) == 0:
            raise SinStock("No hay unidades disponibles de ninguno de los productos del pedido. "
                           "No se hizo ningún cobro; el pedido sigue pendiente.")

        faltantes = []
        for l, c in confirmadas:
            con.execute("UPDATE pedido_items SET cantidad = ? WHERE pedido_id = ? AND producto_id = ?",
                        (c, pedido_id, l["producto_id"]))
            con.execute("UPDATE productos SET stock_disponible = stock_disponible - ?,"
                        " stock_reservado = stock_reservado + ? WHERE id = ?",
                        (c, c, l["producto_id"]))
            if c < l["cantidad_pedida"]:
                faltantes.append(f"{l['sku']} (pediste {l['cantidad_pedida']}, enviamos {c})")
        peso = sum(l["peso_unitario_g"] * c for l, c in confirmadas)
        con.execute("UPDATE pedidos SET pagado_en = ?, peso_teorico_g = ? WHERE id = ?",
                    (fmt(ahora), peso, pedido_id))
        _bitacora(con, ahora, "PAGO_APROBADO",
                  f"Pago simulado aprobado. Peso teórico {peso:g} g."
                  + (f" Sin stock suficiente: {'; '.join(faltantes)}." if faltantes else ""),
                  pedido_id)

        for l, _ in confirmadas:  # alerta de stock bajo (una sola vez mientras siga pendiente)
            disponible = con.execute("SELECT stock_disponible FROM productos WHERE id = ?",
                                     (l["producto_id"],)).fetchone()[0]
            ya = con.execute("SELECT 1 FROM alertas WHERE tipo = 'STOCK_BAJO' AND atendida = 0"
                             " AND producto_id = ?", (l["producto_id"],)).fetchone()
            if disponible <= l["stock_minimo"] and not ya:
                _alerta(con, ahora, "STOCK_BAJO",
                        f"{l['sku']} quedó en {disponible} (mínimo {l['stock_minimo']}).",
                        producto_id=l["producto_id"])

        aviso = (f"Pago recibido. Tu pedido #{pedido_id} sigue con lo disponible; no hubo "
                 f"suficiente de: {'; '.join(faltantes)}." if faltantes
                 else f"Pago recibido. Tu pedido #{pedido_id} está completo.")
        _notificar(con, ahora, pedido_id, aviso)
        libre = con.execute(
            "SELECT * FROM casilleros WHERE estado = 'LIBRE' ORDER BY id LIMIT 1").fetchone()
        if libre:
            _asignar(con, ahora, pedido_id, libre)
        else:
            _cambiar_estado(con, ahora, pedido_id, "EN_COLA", "Sin casillero libre.")
            posicion = con.execute("SELECT COUNT(*) FROM pedidos WHERE estado = 'EN_COLA'"
                                   ).fetchone()[0]
            _notificar(con, ahora, pedido_id,
                       f"Tu pedido #{pedido_id} está en cola (posición {posicion}). "
                       "Te avisamos cuando tenga casillero.")
    return {**obtener_pedido(con, pedido_id), "pago_aprobado": True}


def obtener_pedido(con, pedido_id):
    p = _pedido_fila(con, pedido_id)
    items = [dict(f) for f in con.execute(
        "SELECT pr.sku, pr.nombre, i.cantidad, i.cantidad_pedida, i.peso_unitario_g"
        " FROM pedido_items i"
        " JOIN productos pr ON pr.id = i.producto_id WHERE i.pedido_id = ? ORDER BY pr.sku",
        (pedido_id,))]
    casillero = None
    if p["casillero_id"]:
        casillero = con.execute("SELECT codigo FROM casilleros WHERE id = ?",
                                (p["casillero_id"],)).fetchone()[0]
    posicion = None
    if p["estado"] == "EN_COLA":
        posicion = con.execute("SELECT COUNT(*) FROM pedidos WHERE estado = 'EN_COLA'"
                               " AND id <= ?", (pedido_id,)).fetchone()[0]
    pin = con.execute("SELECT expira_en FROM pins WHERE pedido_id = ? AND estado = 'VIGENTE'",
                      (pedido_id,)).fetchone()
    return {
        "id": p["id"], "estado": p["estado"], "cliente_nombre": p["cliente_nombre"],
        "items": items, "peso_teorico_g": p["peso_teorico_g"], "casillero": casillero,
        "posicion_en_cola": posicion, "pin_expira_en": pin["expira_en"] if pin else None,
        "creado_en": p["creado_en"], "pagado_en": p["pagado_en"],
        # Lo que pidió el cliente y no se pudo confirmar al pagar (RF-05).
        "faltantes": [{"sku": i["sku"], "nombre": i["nombre"], "pedidas": i["cantidad_pedida"],
                       "confirmadas": i["cantidad"]} for i in items
                      if p["pagado_en"] and i["cantidad"] < i["cantidad_pedida"]],
    }


def listar_notificaciones(con, pedido_id):
    _pedido_fila(con, pedido_id)
    return [dict(f) for f in con.execute(
        "SELECT mensaje, creado_en FROM notificaciones WHERE pedido_id = ? ORDER BY id",
        (pedido_id,))]


def listar_bitacora(con, pedido_id):
    _pedido_fila(con, pedido_id)
    return [dict(f) for f in con.execute(
        "SELECT tipo, detalle, creado_en FROM bitacora WHERE pedido_id = ? ORDER BY id",
        (pedido_id,))]


def cancelar_pedido(con, pedido_id, ahora=None, por_admin=False):
    """El cliente solo cancela antes de que la banda mueva la caja. El administrador además
    puede cancelar un pedido ATASCADO (la caja se retira a mano de la banda)."""
    ahora = ahora or ahora_utc()
    permitidos = (("PENDIENTE_PAGO", "EN_COLA", "ASIGNADO", "ATASCADO") if por_admin
                  else ("PENDIENTE_PAGO", "EN_COLA", "ASIGNADO"))
    with transaccion(con):
        pedido = _pedido_fila(con, pedido_id)
        if pedido["estado"] not in permitidos:
            raise EstadoInvalido(
                f"El pedido {pedido_id} está {pedido['estado']} y ya no se puede cancelar.")
        quien = "el administrador" if por_admin else "el cliente"
        _cambiar_estado(con, ahora, pedido_id, "CANCELADO", f"Cancelado por {quien}.")
        if pedido["pagado_en"]:  # un pedido sin pagar nunca descontó inventario
            _liberar_stock(con, pedido_id, devolver=True)
        if pedido["estado"] == "ATASCADO":
            _atender_alertas(con, ahora, "ATASCO", pedido_id)
            _bitacora(con, ahora, "BANDA_ESTADO", "DETENIDA_ATASCO -> REPOSO (caja retirada)",
                      pedido_id, pedido["casillero_id"])
        if pedido["casillero_id"]:
            _set_casillero(con, ahora, pedido["casillero_id"], "LIBRE")
            _despachar_cola(con, ahora)
    return obtener_pedido(con, pedido_id)


def listar_pedidos(con, limite=100):
    """Pedidos más recientes primero (vista del administrador)."""
    ids = [f[0] for f in con.execute("SELECT id FROM pedidos ORDER BY id DESC LIMIT ?", (limite,))]
    return [obtener_pedido(con, i) for i in ids]


def listar_pedidos_de_usuario(con, usuario_id, limite=50):
    """Pedidos de una cuenta de cliente, más recientes primero."""
    ids = [f[0] for f in con.execute(
        "SELECT id FROM pedidos WHERE usuario_id = ? ORDER BY id DESC LIMIT ?", (usuario_id, limite))]
    return [obtener_pedido(con, i) for i in ids]


def dueno_de_pedido(con, pedido_id):
    return _pedido_fila(con, pedido_id)["usuario_id"]
