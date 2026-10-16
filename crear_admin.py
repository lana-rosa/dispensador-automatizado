"""Crea una cuenta de administrador (Issue 1).

    python crear_admin.py

Pide correo, usuario y contraseña en la terminal. Los administradores no se pueden registrar
desde la página: solo quien tiene acceso al computador donde corre el sistema puede crearlos.
"""
import getpass
import sys
from pathlib import Path

import cuentas
import db
from servicio import ErrorNegocio


def main():
    if not Path(db.RUTA_BD).exists():
        db.inicializar().close()
        print("Se creó la base de datos con los datos de demostración.")
    print("== Nuevo administrador ==")
    correo = input("Correo: ").strip()
    usuario = input("Usuario: ").strip()
    contrasena = getpass.getpass("Contraseña (no se ve al escribir, mínimo 8): ")
    if contrasena != getpass.getpass("Repite la contraseña: "):
        sys.exit("Las contraseñas no coinciden. No se creó la cuenta.")
    con = db.conectar()
    try:
        admin = cuentas.crear_usuario(con, correo, usuario, contrasena, rol="ADMIN")
    except ErrorNegocio as e:
        sys.exit(f"No se creó la cuenta: {e.mensaje}")
    finally:
        con.close()
    print(f"Listo: administrador «{admin['usuario']}» creado. Inicia sesión en /admin.")


if __name__ == "__main__":
    main()
