"""Retiro del pedido: PIN, apertura del casillero, caducidad a las 24 h y reemisión del PIN."""

import hashlib
import hmac
import maquina
import secrets
from datetime import timedelta

from .nucleo import (
    EstadoInvalido, ahora_utc, fmt, transaccion, _config, _bitacora, _alerta,
    _notificar, _pedido_fila, _casillero_fila, _set_casillero, _cambiar_estado,
)
from .inventario import _liberar_stock
from .casilleros import _despachar_cola
from .pedidos import obtener_pedido
from .banda import _guardar_eventos


def _hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 100_000).hex()


def _emitir_pin(con, ahora, pedido_id, casillero_id, expira_en=None):
    """Genera un PIN numérico aleatorio (secrets, no random), guarda solo su hash y
    devuelve (pin_en_claro, expira_en). El PIN en claro solo sale por la notificación."""
    longitud = int(_config(con, "longitud_pin"))
    pin = "".join(secrets.choice("0123456789") for _ in range(longitud))
    salt = secrets.token_hex(8)
    expira = expira_en or fmt(ahora + timedelta(hours=int(_config(con, "horas_reserva"))))
    con.execute(
        "INSERT INTO pins (pedido_id, casillero_id, pin_hash, salt, expira_en, creado_en)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (pedido_id, casillero_id, _hash_pin(pin, salt), salt, expira, fmt(ahora)),
    )
    return pin, expira


def abrir_casillero(con, codigo, pin, ahora=None):
    """El cliente digita el PIN en el casillero. Solo abre si es EL vigente de ese casillero.
    Devuelve {"abierto": bool, "motivo": ..., ...}; un PIN malo NO es una excepción porque
    el intento fallido debe quedar guardado."""
    ahora = ahora or ahora_utc()
    with transaccion(con):
        cas = _casillero_fila(con, codigo)
        fila = con.execute("SELECT * FROM pins WHERE casillero_id = ? AND estado = 'VIGENTE'",
                           (cas["id"],)).fetchone()
        if fila is None:
            _bitacora(con, ahora, "PIN_RECHAZADO", "No hay PIN vigente.", None, cas["id"])
            return {"abierto": False, "motivo": "SIN_PIN_VIGENTE"}
        if fila["expira_en"] <= fmt(ahora):
            _caducar(con, ahora, fila)
            _despachar_cola(con, ahora)
            return {"abierto": False, "motivo": "PIN_CADUCADO"}

        max_intentos = int(_config(con, "max_intentos_pin"))
        correcto = hmac.compare_digest(_hash_pin(pin, fila["salt"]), fila["pin_hash"])
        if not correcto:
            intentos = fila["intentos_fallidos"] + 1
            bloquear = intentos >= max_intentos
            con.execute("UPDATE pins SET intentos_fallidos = ?, estado = ? WHERE id = ?",
                        (intentos, "BLOQUEADO" if bloquear else "VIGENTE", fila["id"]))
            _bitacora(con, ahora, "PIN_RECHAZADO", f"PIN incorrecto (intento {intentos}).",
                      fila["pedido_id"], cas["id"])
            if bloquear:
                _alerta(con, ahora, "PIN_BLOQUEADO",
                        f"PIN del pedido {fila['pedido_id']} bloqueado en {codigo} tras "
                        f"{intentos} intentos.", fila["pedido_id"], cas["id"])
                return {"abierto": False, "motivo": "PIN_BLOQUEADO", "intentos_restantes": 0}
            return {"abierto": False, "motivo": "PIN_INCORRECTO",
                    "intentos_restantes": max_intentos - intentos}

        # PIN correcto: se activa la electrocerradura y se cierra el ciclo del pedido.
        con.execute("UPDATE pins SET estado = 'USADO' WHERE id = ?", (fila["id"],))
        _bitacora(con, ahora, "CERRADURA_ABIERTA", f"Casillero {codigo} desbloqueado.",
                  fila["pedido_id"], cas["id"])
        _guardar_eventos(con, ahora, "CERRADURA", maquina.ciclo_cerradura(codigo),
                         fila["pedido_id"], cas["id"])
        _cambiar_estado(con, ahora, fila["pedido_id"], "ENTREGADO", "Retirado por el cliente.")
        _liberar_stock(con, fila["pedido_id"], devolver=False)
        _set_casillero(con, ahora, cas["id"], "LIBRE")
        _despachar_cola(con, ahora)
        return {"abierto": True, "motivo": "OK", "pedido_id": fila["pedido_id"]}


def _caducar(con, ahora, pin_fila):
    """PIN vencido: el pedido caduca, el casillero se libera y el stock vuelve a inventario."""
    con.execute("UPDATE pins SET estado = 'CADUCADO' WHERE id = ?", (pin_fila["id"],))
    _cambiar_estado(con, ahora, pin_fila["pedido_id"], "CADUCADO",
                    "No se retiró dentro del plazo.")
    _liberar_stock(con, pin_fila["pedido_id"], devolver=True)
    _set_casillero(con, ahora, pin_fila["casillero_id"], "LIBRE")
    _notificar(con, ahora, pin_fila["pedido_id"],
               f"Tu pedido #{pin_fila['pedido_id']} caducó porque no se retiró a tiempo.")


def caducar_vencidos(con, ahora=None):
    """Barrido periódico (P5): caduca todo PIN vencido y reasigna los casilleros liberados
    a la cola. Devuelve los ids de pedidos caducados."""
    ahora = ahora or ahora_utc()
    with transaccion(con):
        vencidos = con.execute(
            "SELECT * FROM pins WHERE estado IN ('VIGENTE', 'BLOQUEADO') AND expira_en <= ?"
            " ORDER BY id", (fmt(ahora),)).fetchall()
        for fila in vencidos:
            _caducar(con, ahora, fila)
        if vencidos:
            _despachar_cola(con, ahora)
    return [f["pedido_id"] for f in vencidos]


def reemitir_pin(con, pedido_id, ahora=None):
    """Tras un PIN bloqueado por intentos fallidos, el administrador emite uno nuevo
    (mismo plazo de vencimiento: bloquear no alarga la reserva)."""
    ahora = ahora or ahora_utc()
    with transaccion(con):
        pedido = _pedido_fila(con, pedido_id)
        bloqueado = con.execute(
            "SELECT * FROM pins WHERE pedido_id = ? AND estado = 'BLOQUEADO'"
            " ORDER BY id DESC LIMIT 1", (pedido_id,)).fetchone()
        if pedido["estado"] != "LISTO_RETIRO" or bloqueado is None:
            raise EstadoInvalido(f"El pedido {pedido_id} no tiene un PIN bloqueado.")
        # El PIN bloqueado queda reemplazado. Si siguiera como BLOQUEADO, un segundo clic
        # intentaría emitir otro PIN (error 500) y el barrido de 24 h caducaría dos veces.
        con.execute("UPDATE pins SET estado = 'CADUCADO' WHERE id = ?", (bloqueado["id"],))
        pin, expira = _emitir_pin(con, ahora, pedido_id, pedido["casillero_id"],
                                  expira_en=bloqueado["expira_en"])
        codigo = con.execute("SELECT codigo FROM casilleros WHERE id = ?",
                             (pedido["casillero_id"],)).fetchone()[0]
        _bitacora(con, ahora, "PIN_REEMITIDO", None, pedido_id, pedido["casillero_id"])
        _notificar(con, ahora, pedido_id,
                   f"Se emitió un nuevo PIN para el casillero {codigo}. PIN: {pin}. "
                   f"Vigente hasta {expira} (UTC).")
        # La alerta queda resuelta: así el administrador ve que su acción tuvo efecto
        # y el botón «Reemitir PIN» no queda disponible para un segundo clic.
        con.execute("UPDATE alertas SET atendida = 1, atendida_en = ? WHERE tipo = 'PIN_BLOQUEADO'"
                    " AND atendida = 0 AND pedido_id = ?", (fmt(ahora), pedido_id))
    return obtener_pedido(con, pedido_id)
