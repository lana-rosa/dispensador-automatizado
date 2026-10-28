"""Piezas comunes: errores de negocio, transiciones de estado, transacciones y ayudas internas."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


FORMATO_FECHA = "%Y-%m-%dT%H:%M:%SZ"


TRANSICIONES = {
    "PENDIENTE_PAGO": {"ASIGNADO", "EN_COLA", "CANCELADO"},
    "EN_COLA": {"ASIGNADO", "CANCELADO"},
    "ASIGNADO": {"EN_TRANSPORTE", "CANCELADO"},
    "EN_TRANSPORTE": {"VERIFICANDO", "ATASCADO"},
    "VERIFICANDO": {"LISTO_RETIRO", "RECHAZADO"},
    "LISTO_RETIRO": {"ENTREGADO", "CADUCADO"},
    "ATASCADO": {"EN_TRANSPORTE", "CANCELADO"},
    "ENTREGADO": set(),
    "RECHAZADO": {"VERIFICANDO"},  # el administrador resolvió el error de despacho
    "CANCELADO": set(),
    "CADUCADO": set(),
}


class ErrorNegocio(Exception):
    """Error esperado (regla incumplida). `status` es el código HTTP sugerido."""

    status = 400
    codigo = "ERROR"

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


class NoEncontrado(ErrorNegocio):
    status, codigo = 404, "NO_ENCONTRADO"


class SinStock(ErrorNegocio):
    status, codigo = 409, "SIN_STOCK"


class EstadoInvalido(ErrorNegocio):
    status, codigo = 409, "ESTADO_INVALIDO"


class SolicitudInvalida(ErrorNegocio):
    status, codigo = 422, "SOLICITUD_INVALIDA"


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def fmt(fecha: datetime) -> str:
    return fecha.strftime(FORMATO_FECHA)


@contextmanager
def transaccion(con: sqlite3.Connection):
    """BEGIN IMMEDIATE toma el candado de escritura desde el inicio: dos pedidos
    simultáneos no pueden quedarse con el mismo casillero ni el mismo stock."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def _config(con, clave: str) -> str:
    return con.execute("SELECT valor FROM configuracion WHERE clave = ?", (clave,)).fetchone()[0]


def _bitacora(con, ahora, tipo, detalle=None, pedido_id=None, casillero_id=None):
    con.execute(
        "INSERT INTO bitacora (pedido_id, casillero_id, tipo, detalle, creado_en)"
        " VALUES (?, ?, ?, ?, ?)",
        (pedido_id, casillero_id, tipo, detalle, fmt(ahora)),
    )


def _alerta(con, ahora, tipo, mensaje, pedido_id=None, casillero_id=None, producto_id=None):
    con.execute(
        "INSERT INTO alertas (tipo, pedido_id, casillero_id, producto_id, mensaje, creado_en)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (tipo, pedido_id, casillero_id, producto_id, mensaje, fmt(ahora)),
    )


def _notificar(con, ahora, pedido_id, mensaje):
    con.execute(
        "INSERT INTO notificaciones (pedido_id, mensaje, creado_en) VALUES (?, ?, ?)",
        (pedido_id, mensaje, fmt(ahora)),
    )


def _pedido_fila(con, pedido_id):
    fila = con.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,)).fetchone()
    if fila is None:
        raise NoEncontrado(f"No existe el pedido {pedido_id}.")
    return fila


def _casillero_fila(con, codigo):
    fila = con.execute("SELECT * FROM casilleros WHERE codigo = ?", (codigo,)).fetchone()
    if fila is None:
        raise NoEncontrado(f"No existe el casillero {codigo}.")
    return fila


def _set_casillero(con, ahora, casillero_id, estado):
    con.execute(
        "UPDATE casilleros SET estado = ?, actualizado_en = ? WHERE id = ?",
        (estado, fmt(ahora), casillero_id),
    )


def _cambiar_estado(con, ahora, pedido_id, nuevo, detalle="", casillero_id=None):
    """Único punto por donde cambia el estado de un pedido: valida la transición y
    deja rastro en la bitácora (trazabilidad)."""
    pedido = _pedido_fila(con, pedido_id)
    anterior = pedido["estado"]
    if nuevo not in TRANSICIONES[anterior]:
        raise EstadoInvalido(f"El pedido {pedido_id} no puede pasar de {anterior} a {nuevo}.")
    con.execute(
        "UPDATE pedidos SET estado = ?, casillero_id = COALESCE(?, casillero_id),"
        " actualizado_en = ? WHERE id = ?",
        (nuevo, casillero_id, fmt(ahora), pedido_id),
    )
    _bitacora(
        con, ahora, "ESTADO", f"{anterior} -> {nuevo}. {detalle}".strip(),
        pedido_id, casillero_id or pedido["casillero_id"],
    )
