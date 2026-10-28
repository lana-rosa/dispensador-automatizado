"""Casilleros: asignación, cola FIFO, mantenimiento y ocupación."""

from .nucleo import (
    EstadoInvalido, ahora_utc, transaccion, _bitacora, _notificar, _casillero_fila,
    _set_casillero, _cambiar_estado,
)


def _asignar(con, ahora, pedido_id, casillero):
    _set_casillero(con, ahora, casillero["id"], "RESERVADO")
    _cambiar_estado(con, ahora, pedido_id, "ASIGNADO",
                    f"Casillero {casillero['codigo']} reservado.", casillero["id"])


def _despachar_cola(con, ahora):
    """Mientras haya casilleros libres y pedidos esperando, asigna el más antiguo."""
    libres = con.execute("SELECT * FROM casilleros WHERE estado = 'LIBRE' ORDER BY id").fetchall()
    for casillero in libres:
        siguiente = con.execute(
            "SELECT id FROM pedidos WHERE estado = 'EN_COLA' ORDER BY id LIMIT 1"
        ).fetchone()
        if siguiente is None:
            break
        _asignar(con, ahora, siguiente["id"], casillero)
        _notificar(con, ahora, siguiente["id"],
                   f"Tu pedido #{siguiente['id']} salió de la cola y ya tiene casillero.")


def estado_casilleros(con):
    filas = [dict(f) for f in con.execute(
        "SELECT c.codigo, c.estado, p.id AS pedido_id FROM casilleros c"
        " LEFT JOIN pedidos p ON p.casillero_id = c.id AND p.estado IN"
        " ('ASIGNADO','EN_TRANSPORTE','VERIFICANDO','LISTO_RETIRO','ATASCADO')"
        " ORDER BY c.id")]
    total = len(filas)
    en_uso = sum(1 for f in filas if f["estado"] in ("RESERVADO", "OCUPADO"))
    en_cola = con.execute("SELECT COUNT(*) FROM pedidos WHERE estado = 'EN_COLA'").fetchone()[0]
    return {"casilleros": filas, "total": total, "en_uso": en_uso,
            "ocupacion_pct": round(100 * en_uso / total, 1) if total else 0.0,
            "pedidos_en_cola": en_cola}


def cambiar_mantenimiento(con, codigo, activar: bool, ahora=None):
    """El administrador saca un casillero de servicio (solo si está libre) o lo devuelve
    (y entonces se reparte a la cola si hay pedidos esperando)."""
    ahora = ahora or ahora_utc()
    with transaccion(con):
        cas = _casillero_fila(con, codigo)
        if activar:
            if cas["estado"] != "LIBRE":
                raise EstadoInvalido(f"El casillero {codigo} está {cas['estado']}; "
                                     "solo se pone en mantenimiento si está LIBRE.")
            _set_casillero(con, ahora, cas["id"], "MANTENIMIENTO")
        else:
            if cas["estado"] != "MANTENIMIENTO":
                raise EstadoInvalido(f"El casillero {codigo} no está en mantenimiento.")
            _set_casillero(con, ahora, cas["id"], "LIBRE")
            _despachar_cola(con, ahora)
        _bitacora(con, ahora, "MANTENIMIENTO", f"{codigo} {'ON' if activar else 'OFF'}",
                  casillero_id=cas["id"])
    return estado_casilleros(con)


def listar_codigos_casilleros(con):
    return [f[0] for f in con.execute("SELECT codigo FROM casilleros ORDER BY id")]
