"""Verificación del peso medido (y foto simulada) y falla de la báscula."""

import math

from .nucleo import (
    EstadoInvalido, SolicitudInvalida, ahora_utc, fmt, transaccion, _config, _bitacora,
    _alerta, _notificar, _pedido_fila, _set_casillero, _cambiar_estado,
)
from .alertas import _atender_alertas
from .inventario import _liberar_stock, _reservar_de_nuevo
from .pedidos import obtener_pedido
from .retiro import _emitir_pin


def _validar_lectura_bascula(con, pedido_id, peso_medido_g, ahora):
    """Una lectura imposible (sin dato, negativa, NaN o por encima del máximo de la celda de
    carga) es una FALLA DE LA BÁSCULA, no un peso distinto al esperado: se avisa al
    administrador y se rechaza con 422 SIN cambiar el pedido (sigue en VERIFICANDO y se puede
    reintentar). La alerta se guarda en su propia transacción antes de lanzar el error, porque
    una excepción dentro de la transacción deshace todo."""
    maximo = float(_config(con, "peso_max_bascula_g"))
    if peso_medido_g is None:
        motivo = "la báscula no envió ningún peso"
    elif not math.isfinite(peso_medido_g):
        motivo = f"la báscula envió un valor inválido ({peso_medido_g})"
    elif peso_medido_g < 0:
        motivo = f"la báscula envió un peso negativo ({peso_medido_g:g} g)"
    elif peso_medido_g > maximo:
        motivo = f"la báscula envió {peso_medido_g:g} g, por encima del máximo de {maximo:g} g"
    else:
        return
    with transaccion(con):
        pedido = _pedido_fila(con, pedido_id)
        ya = con.execute("SELECT 1 FROM alertas WHERE tipo = 'BASCULA' AND atendida = 0"
                         " AND pedido_id = ?", (pedido_id,)).fetchone()
        if not ya:
            _alerta(con, ahora, "BASCULA",
                    f"Pedido {pedido_id}: {motivo}. Revisar la celda de carga.",
                    pedido_id, pedido["casillero_id"])
        _bitacora(con, ahora, "BASCULA_FALLA", motivo, pedido_id, pedido["casillero_id"])
    raise SolicitudInvalida(f"Lectura de báscula inválida: {motivo}. Se avisó al administrador.")


def registrar_verificacion(con, pedido_id, peso_medido_g, foto_ruta, ahora=None):
    """Valida el peso medido contra el teórico y guarda la foto simulada.
    Aprobado -> genera PIN y notifica. Rechazado -> sin PIN, alerta al administrador."""
    ahora = ahora or ahora_utc()
    _validar_lectura_bascula(con, pedido_id, peso_medido_g, ahora)
    with transaccion(con):
        pedido = _pedido_fila(con, pedido_id)
        if pedido["estado"] != "VERIFICANDO":
            raise EstadoInvalido(
                f"El pedido {pedido_id} está {pedido['estado']}; solo se verifica en VERIFICANDO.")
        teorico = pedido["peso_teorico_g"]
        tolerancia = max(float(_config(con, "tolerancia_peso_min_g")),
                         teorico * float(_config(con, "tolerancia_peso_pct")) / 100)
        aprobado = abs(peso_medido_g - teorico) <= tolerancia
        con.execute(
            "INSERT INTO verificaciones (pedido_id, peso_teorico_g, peso_medido_g, tolerancia_g,"
            " aprobado, foto_ruta, creado_en) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pedido_id, teorico, peso_medido_g, tolerancia, int(aprobado), foto_ruta, fmt(ahora)),
        )
        cas_id = pedido["casillero_id"]
        codigo = con.execute("SELECT codigo FROM casilleros WHERE id = ?", (cas_id,)).fetchone()[0]

        if aprobado:
            pin, expira = _emitir_pin(con, ahora, pedido_id, cas_id)
            _set_casillero(con, ahora, cas_id, "OCUPADO")
            _cambiar_estado(con, ahora, pedido_id, "LISTO_RETIRO",
                            f"Peso OK ({peso_medido_g:g} g). PIN emitido.")
            _notificar(con, ahora, pedido_id,
                       f"Tu pedido #{pedido_id} está listo en el casillero {codigo}. "
                       f"PIN: {pin}. Vigente hasta {expira} (UTC).")
        else:
            # Error de Despacho: sin PIN. El casillero queda en mantenimiento hasta que el
            # administrador lo revise y el stock se devuelve a disponible para revisarlo.
            _cambiar_estado(con, ahora, pedido_id, "RECHAZADO",
                            f"Peso medido {peso_medido_g:g} g vs teórico {teorico:g} g.")
            _liberar_stock(con, pedido_id, devolver=True)
            _set_casillero(con, ahora, cas_id, "MANTENIMIENTO")
            _alerta(con, ahora, "ERROR_DESPACHO",
                    f"Pedido {pedido_id}: peso medido {peso_medido_g:g} g, teórico {teorico:g} g "
                    f"(tolerancia ±{tolerancia:g} g). Casillero {codigo} en mantenimiento.",
                    pedido_id, cas_id)
            _notificar(con, ahora, pedido_id,
                       f"No pudimos despachar tu pedido #{pedido_id}. Un asesor te contactará.")
        estado = _pedido_fila(con, pedido_id)["estado"]
    return {"aprobado": aprobado, "peso_teorico_g": teorico, "peso_medido_g": peso_medido_g,
            "tolerancia_g": tolerancia, "estado_pedido": estado}


def resolver_error_despacho(con, pedido_id, ahora=None):
    """El administrador revisó la caja, corrigió el problema (faltaba o sobraba un producto, la
    báscula estaba mal calibrada...) y quiere CONTINUAR. El pedido NO salta directo al PIN: vuelve
    a VERIFICANDO para pesar de nuevo, así la regla del peso sigue siendo la que decide.
    Devuelve el casillero de MANTENIMIENTO a RESERVADO, vuelve a reservar el stock que se había
    devuelto y deja atendida la alerta."""
    ahora = ahora or ahora_utc()
    with transaccion(con):
        pedido = _pedido_fila(con, pedido_id)
        if pedido["estado"] != "RECHAZADO":
            raise EstadoInvalido(
                f"El pedido {pedido_id} está {pedido['estado']}; solo se continúa un pedido "
                "con error de despacho (RECHAZADO).")
        cas = con.execute("SELECT * FROM casilleros WHERE id = ?",
                          (pedido["casillero_id"],)).fetchone()
        if cas["estado"] != "MANTENIMIENTO":
            raise EstadoInvalido(
                f"El casillero {cas['codigo']} ya no está en mantenimiento ({cas['estado']}); "
                "no se puede continuar este pedido.")
        _reservar_de_nuevo(con, pedido_id)
        _cambiar_estado(con, ahora, pedido_id, "VERIFICANDO",
                        "Problema solucionado por el administrador; falta volver a pesar.")
        _set_casillero(con, ahora, cas["id"], "RESERVADO")
        _atender_alertas(con, ahora, "ERROR_DESPACHO", pedido_id)
        _bitacora(con, ahora, "ERROR_DESPACHO_RESUELTO",
                  "El administrador marcó el problema como solucionado.", pedido_id, cas["id"])
        _notificar(con, ahora, pedido_id,
                   f"Resolvimos el problema con tu pedido #{pedido_id}. Lo estamos verificando "
                   "de nuevo y te avisamos en cuanto esté listo.")
    return obtener_pedido(con, pedido_id)
