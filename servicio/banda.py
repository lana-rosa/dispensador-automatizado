"""Banda transportadora simulada: despacho por sensores, atascos y estado de la máquina."""

import maquina
from datetime import timedelta

from .nucleo import (
    EstadoInvalido, SolicitudInvalida, ahora_utc, transaccion, _config, _bitacora,
    _alerta, _pedido_fila, _cambiar_estado,
)
from .alertas import _atender_alertas
from .pedidos import obtener_pedido


def _guardar_eventos(con, ahora, prefijo, eventos, pedido_id, casillero_id):
    """Pasa los eventos de una máquina de estados a la bitácora, cada uno con su segundo."""
    for ev in eventos:
        _bitacora(con, ahora + timedelta(seconds=ev.t), f"{prefijo}_{ev.tipo}",
                  f"t={ev.t:g}s {ev.detalle}", pedido_id, casillero_id)


def pedido_atascado(con):
    """Mientras un pedido esté ATASCADO la banda permanece detenida."""
    fila = con.execute("SELECT id FROM pedidos WHERE estado = 'ATASCADO' LIMIT 1").fetchone()
    return fila["id"] if fila else None


def despachar_pedido(con, pedido_id, atasco_en=None, ahora=None):
    """Corre la máquina de estados de la banda para llevar la caja al casillero asignado.

    Acepta pedidos ASIGNADO (primer intento) o ATASCADO (reintento tras despejar la banda).
    Éxito  -> EN_TRANSPORTE -> VERIFICANDO (sigue la validación de peso y foto).
    Atasco -> EN_TRANSPORTE -> ATASCADO, motores apagados y alerta ATASCO al administrador.
    """
    ahora = ahora or ahora_utc()
    with transaccion(con):
        pedido = _pedido_fila(con, pedido_id)
        if pedido["estado"] not in ("ASIGNADO", "ATASCADO"):
            raise EstadoInvalido(f"El pedido {pedido_id} está {pedido['estado']}; solo se "
                                 "despachan pedidos ASIGNADO o ATASCADO (reintento).")
        otro = pedido_atascado(con)
        if otro is not None and otro != pedido_id:
            raise EstadoInvalido(f"La banda está detenida por un atasco del pedido {otro}. "
                                 "Reinténtelo o cancélelo primero.")
        reintento = pedido["estado"] == "ATASCADO"
        cas_id = pedido["casillero_id"]
        codigo = con.execute("SELECT codigo FROM casilleros WHERE id = ?", (cas_id,)).fetchone()[0]
        timeout = float(_config(con, "timeout_atasco_s"))
        try:
            rec = maquina.simular_recorrido(codigo, timeout, atasco_en)
        except ValueError as e:
            raise SolicitudInvalida(str(e))

        _cambiar_estado(con, ahora, pedido_id, "EN_TRANSPORTE",
                        "Reintento tras atasco." if reintento else "Banda en marcha.")
        _guardar_eventos(con, ahora, "BANDA", rec.eventos, pedido_id, cas_id)
        fin = ahora + timedelta(seconds=rec.duracion_s)
        if rec.exito:
            _cambiar_estado(con, fin, pedido_id, "VERIFICANDO",
                            f"Caja en el casillero {codigo} ({rec.duracion_s:g} s).")
            if reintento:
                _atender_alertas(con, fin, "ATASCO", pedido_id)
        else:
            _cambiar_estado(con, fin, pedido_id, "ATASCADO",
                            f"{rec.sensor_fallido} sin detección tras {timeout:g} s.")
            _alerta(con, fin, "ATASCO",
                    f"Pedido {pedido_id}: la caja no llegó a {rec.sensor_fallido} tras {timeout:g} s "
                    f"(rumbo al casillero {codigo}). Motores detenidos. Despeje la banda y pulse "
                    "Reintentar, o cancele el pedido.", pedido_id, cas_id)
    return {
        "pedido": obtener_pedido(con, pedido_id),
        "exito": rec.exito,
        "estado_banda": rec.estado_final.value,
        "sensor_fallido": rec.sensor_fallido,
        "duracion_s": rec.duracion_s,
        "eventos": [{"t": e.t, "tipo": e.tipo, "detalle": e.detalle} for e in rec.eventos],
    }


def estado_maquina(con, limite=25):
    """Estado actual de la banda y últimas señales (para la pantalla del administrador)."""
    atascado = pedido_atascado(con)
    senales = [dict(f) for f in con.execute(
        "SELECT pedido_id, tipo, detalle, creado_en FROM bitacora"
        " WHERE tipo LIKE 'BANDA_%' OR tipo LIKE 'CERRADURA_%' ORDER BY id DESC LIMIT ?",
        (limite,))]
    return {"banda": "DETENIDA_ATASCO" if atascado else "REPOSO",
            "pedido_atascado": atascado, "senales": senales}
