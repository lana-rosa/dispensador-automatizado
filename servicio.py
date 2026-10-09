"""Lógica de negocio del dispensador.

Aquí viven TODAS las reglas (stock, cola, PIN, peso, caducidad). La API solo traduce
HTTP <-> estas funciones, y la simulación del paso 3 las reutilizará.

Convenciones:
  * Cada función pública recibe `ahora` (datetime UTC) para poder simular el paso del
    tiempo en las pruebas (P5: "pasaron 24 h") sin esperar.
  * Cada función pública que escribe corre dentro de UNA transacción: o se guarda todo
    o no se guarda nada. Las funciones que empiezan con "_" son internas y no abren
    transacción propia.
"""
import hashlib
import hmac
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

FORMATO_FECHA = "%Y-%m-%dT%H:%M:%SZ"

# Qué cambios de estado de un pedido son legales. Cualquier otro se rechaza.
TRANSICIONES = {
    "EN_COLA": {"ASIGNADO", "CANCELADO"},
    "ASIGNADO": {"EN_TRANSPORTE", "CANCELADO"},
    "EN_TRANSPORTE": {"VERIFICANDO", "ATASCADO"},
    "VERIFICANDO": {"LISTO_RETIRO", "RECHAZADO"},
    "LISTO_RETIRO": {"ENTREGADO", "CADUCADO"},
    "ATASCADO": {"EN_TRANSPORTE", "CANCELADO"},
    "ENTREGADO": set(),
    "RECHAZADO": set(),
    "CANCELADO": set(),
    "CADUCADO": set(),
}


# ------------------------------------------------------------------ errores
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


# ------------------------------------------------------------------ utilidades
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


# ------------------------------------------------------------------ stock
def _comprometer_stock(con, pedido_id):
    """Disponible -> reservado (al aceptar el pedido)."""
    for it in con.execute("SELECT producto_id, cantidad FROM pedido_items WHERE pedido_id = ?",
                          (pedido_id,)):
        con.execute(
            "UPDATE productos SET stock_disponible = stock_disponible - ?,"
            " stock_reservado = stock_reservado + ? WHERE id = ?",
            (it["cantidad"], it["cantidad"], it["producto_id"]),
        )


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


# ------------------------------------------------------------------ cola y casilleros
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


# ------------------------------------------------------------------ pedidos
def crear_pedido(con, cliente_nombre, cliente_contacto, items, ahora=None):
    """items: lista de (sku, cantidad). Acepta el pedido si hay stock de TODO el carrito;
    lo asigna a un casillero libre o, si no hay, lo deja en cola."""
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
            if prod["stock_disponible"] < cantidad:
                raise SinStock(f"Stock insuficiente de {sku}: pides {cantidad} y quedan "
                               f"{prod['stock_disponible']}.")
            lineas.append((prod, cantidad))

        peso_teorico = sum(p["peso_unitario_g"] * c for p, c in lineas)
        pedido_id = con.execute(
            "INSERT INTO pedidos (cliente_nombre, cliente_contacto, peso_teorico_g,"
            " creado_en, actualizado_en) VALUES (?, ?, ?, ?, ?)",
            (cliente_nombre, cliente_contacto, peso_teorico, fmt(ahora), fmt(ahora)),
        ).lastrowid
        for prod, cantidad in lineas:
            con.execute(
                "INSERT INTO pedido_items (pedido_id, producto_id, cantidad, peso_unitario_g)"
                " VALUES (?, ?, ?, ?)",
                (pedido_id, prod["id"], cantidad, prod["peso_unitario_g"]),
            )
        _comprometer_stock(con, pedido_id)
        _bitacora(con, ahora, "PEDIDO_CREADO",
                  f"{len(lineas)} producto(s), peso teórico {peso_teorico:g} g.", pedido_id)

        for prod, _ in lineas:  # alerta de stock bajo (una sola vez mientras siga pendiente)
            disponible = con.execute("SELECT stock_disponible FROM productos WHERE id = ?",
                                     (prod["id"],)).fetchone()[0]
            ya = con.execute("SELECT 1 FROM alertas WHERE tipo = 'STOCK_BAJO' AND atendida = 0"
                             " AND producto_id = ?", (prod["id"],)).fetchone()
            if disponible <= prod["stock_minimo"] and not ya:
                _alerta(con, ahora, "STOCK_BAJO",
                        f"{prod['sku']} quedó en {disponible} (mínimo {prod['stock_minimo']}).",
                        producto_id=prod["id"])

        libre = con.execute(
            "SELECT * FROM casilleros WHERE estado = 'LIBRE' ORDER BY id LIMIT 1").fetchone()
        if libre:
            _asignar(con, ahora, pedido_id, libre)
        else:
            posicion = con.execute("SELECT COUNT(*) FROM pedidos WHERE estado = 'EN_COLA'"
                                   ).fetchone()[0]
            _notificar(con, ahora, pedido_id,
                       f"Tu pedido #{pedido_id} fue aceptado y está en cola (posición {posicion}). "
                       "Te avisamos cuando tenga casillero.")
    return obtener_pedido(con, pedido_id)


def obtener_pedido(con, pedido_id):
    p = _pedido_fila(con, pedido_id)
    items = [dict(f) for f in con.execute(
        "SELECT pr.sku, pr.nombre, i.cantidad, i.peso_unitario_g FROM pedido_items i"
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
        "creado_en": p["creado_en"],
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


def cancelar_pedido(con, pedido_id, ahora=None):
    """Solo se puede cancelar antes de que la banda mueva la caja."""
    ahora = ahora or ahora_utc()
    with transaccion(con):
        pedido = _pedido_fila(con, pedido_id)
        if pedido["estado"] not in ("EN_COLA", "ASIGNADO"):
            raise EstadoInvalido(
                f"El pedido {pedido_id} está {pedido['estado']} y ya no se puede cancelar.")
        _cambiar_estado(con, ahora, pedido_id, "CANCELADO", "Cancelado por el cliente.")
        _liberar_stock(con, pedido_id, devolver=True)
        if pedido["casillero_id"]:
            _set_casillero(con, ahora, pedido["casillero_id"], "LIBRE")
            _despachar_cola(con, ahora)
    return obtener_pedido(con, pedido_id)


# ------------------------------------------------------------------ transporte (provisional)
def simular_llegada_a_casillero(con, pedido_id, ahora=None):
    """ATAJO del paso 2: lleva el pedido ASIGNADO -> EN_TRANSPORTE -> VERIFICANDO como si la
    banda ya hubiera entregado la caja. En el paso 3 lo reemplaza la máquina de estados."""
    ahora = ahora or ahora_utc()
    with transaccion(con):
        _cambiar_estado(con, ahora, pedido_id, "EN_TRANSPORTE", "(simulado) banda en marcha.")
        _cambiar_estado(con, ahora, pedido_id, "VERIFICANDO", "(simulado) caja en el casillero.")
    return obtener_pedido(con, pedido_id)


# ------------------------------------------------------------------ PIN
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


# ------------------------------------------------------------------ peso + foto
def registrar_verificacion(con, pedido_id, peso_medido_g, foto_ruta, ahora=None):
    """Valida el peso medido contra el teórico y guarda la foto simulada.
    Aprobado -> genera PIN y notifica. Rechazado -> sin PIN, alerta al administrador."""
    ahora = ahora or ahora_utc()
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


# ------------------------------------------------------------------ administración
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


def inventario(con):
    return [dict(f) for f in con.execute(
        "SELECT sku, nombre, categoria, peso_unitario_g, stock_disponible, stock_reservado,"
        " stock_minimo, (stock_disponible <= stock_minimo) AS stock_bajo"
        " FROM productos WHERE activo = 1 ORDER BY sku")]


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
        pin, expira = _emitir_pin(con, ahora, pedido_id, pedido["casillero_id"],
                                  expira_en=bloqueado["expira_en"])
        codigo = con.execute("SELECT codigo FROM casilleros WHERE id = ?",
                             (pedido["casillero_id"],)).fetchone()[0]
        _bitacora(con, ahora, "PIN_REEMITIDO", None, pedido_id, pedido["casillero_id"])
        _notificar(con, ahora, pedido_id,
                   f"Se emitió un nuevo PIN para el casillero {codigo}. PIN: {pin}. "
                   f"Vigente hasta {expira} (UTC).")
    return obtener_pedido(con, pedido_id)


def listar_pedidos(con, limite=100):
    """Pedidos más recientes primero (vista del administrador)."""
    ids = [f[0] for f in con.execute("SELECT id FROM pedidos ORDER BY id DESC LIMIT ?", (limite,))]
    return [obtener_pedido(con, i) for i in ids]


def listar_codigos_casilleros(con):
    return [f[0] for f in con.execute("SELECT codigo FROM casilleros ORDER BY id")]
