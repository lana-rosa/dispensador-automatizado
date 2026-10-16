"""Cuentas de usuario y sesiones (Issue 1).

  * Clientes: se registran con correo, usuario y contraseña desde la pantalla del cliente.
  * Administradores: se crean con `python crear_admin.py`; no existe registro público de admins.
  * Contraseñas: PBKDF2-SHA256 con sal aleatoria por usuario; nunca se guardan en claro.
  * Sesiones: al iniciar sesión se entrega un token aleatorio; la BD guarda solo su hash.
"""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from servicio import (ErrorNegocio, SolicitudInvalida, _config, ahora_utc, fmt, transaccion)

ITERACIONES = 200_000
RE_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RE_USUARIO = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
MIN_CONTRASENA = 8


class NoAutenticado(ErrorNegocio):
    status, codigo = 401, "NO_AUTENTICADO"


class SinPermiso(ErrorNegocio):
    status, codigo = 403, "SIN_PERMISO"


class YaExiste(ErrorNegocio):
    status, codigo = 409, "YA_EXISTE"


def _hash_contrasena(contrasena: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", contrasena.encode(), bytes.fromhex(salt), ITERACIONES).hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _validar(correo: str, usuario: str, contrasena: str):
    if not RE_CORREO.match(correo or ""):
        raise SolicitudInvalida("El correo no tiene un formato válido.")
    if not RE_USUARIO.match(usuario or ""):
        raise SolicitudInvalida("El usuario debe tener de 3 a 30 caracteres: letras, números, "
                                "punto, guion o guion bajo.")
    if len(contrasena or "") < MIN_CONTRASENA:
        raise SolicitudInvalida(f"La contraseña debe tener al menos {MIN_CONTRASENA} caracteres.")


def _publico(fila) -> dict:
    return {"id": fila["id"], "correo": fila["correo"], "usuario": fila["usuario"], "rol": fila["rol"]}


def crear_usuario(con, correo, usuario, contrasena, rol="CLIENTE", ahora=None) -> dict:
    ahora = ahora or ahora_utc()
    correo, usuario = (correo or "").strip().lower(), (usuario or "").strip()
    _validar(correo, usuario, contrasena)
    if rol not in ("CLIENTE", "ADMIN"):
        raise SolicitudInvalida("Rol inválido.")
    salt = secrets.token_hex(16)
    with transaccion(con):
        repetido = con.execute("SELECT correo, usuario FROM usuarios WHERE correo = ? OR usuario = ?",
                               (correo, usuario)).fetchone()
        if repetido:
            campo = "correo" if repetido["correo"].lower() == correo else "usuario"
            raise YaExiste(f"Ya existe una cuenta con ese {campo}.")
        uid = con.execute(
            "INSERT INTO usuarios (correo, usuario, password_hash, salt, rol, creado_en)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (correo, usuario, _hash_contrasena(contrasena, salt), salt, rol, fmt(ahora))).lastrowid
    return _publico(con.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone())


def iniciar_sesion(con, identificador, contrasena, ahora=None) -> dict:
    """identificador: correo o usuario. Devuelve {"token", "expira_en", "usuario"}."""
    ahora = ahora or ahora_utc()
    fila = con.execute("SELECT * FROM usuarios WHERE (correo = ? OR usuario = ?) AND activo = 1",
                       ((identificador or "").strip().lower(), (identificador or "").strip())).fetchone()
    # Se calcula el hash aunque el usuario no exista, para no revelar qué cuentas existen
    # por diferencia de tiempo de respuesta.
    salt = fila["salt"] if fila else "00" * 16
    calculado = _hash_contrasena(contrasena or "", salt)
    if fila is None or not hmac.compare_digest(calculado, fila["password_hash"]):
        raise NoAutenticado("Usuario o contraseña incorrectos.")
    token = secrets.token_urlsafe(32)
    expira = fmt(ahora + timedelta(hours=int(_config(con, "horas_sesion"))))
    with transaccion(con):
        con.execute("DELETE FROM sesiones WHERE usuario_id = ? AND expira_en <= ?",
                    (fila["id"], fmt(ahora)))
        con.execute("INSERT INTO sesiones (token_hash, usuario_id, creado_en, expira_en)"
                    " VALUES (?, ?, ?, ?)", (_hash_token(token), fila["id"], fmt(ahora), expira))
    return {"token": token, "expira_en": expira, "usuario": _publico(fila)}


def usuario_por_token(con, token, ahora=None) -> dict:
    ahora = ahora or ahora_utc()
    if not token:
        raise NoAutenticado("Debes iniciar sesión.")
    fila = con.execute(
        "SELECT u.* FROM sesiones s JOIN usuarios u ON u.id = s.usuario_id"
        " WHERE s.token_hash = ? AND s.expira_en > ? AND u.activo = 1",
        (_hash_token(token), fmt(ahora))).fetchone()
    if fila is None:
        raise NoAutenticado("La sesión no es válida o ya venció. Inicia sesión de nuevo.")
    return _publico(fila)


def cerrar_sesion(con, token):
    with transaccion(con):
        con.execute("DELETE FROM sesiones WHERE token_hash = ?", (_hash_token(token or ""),))
