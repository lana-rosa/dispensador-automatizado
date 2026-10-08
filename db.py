"""Acceso a la base de datos del dispensador (SQLite, solo librería estándar)."""
import sqlite3
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
RUTA_ESQUEMA = CARPETA / "schema.sql"
RUTA_BD = CARPETA / "dispensador.db"

PRODUCTOS_DEMO = [
    # sku, nombre, categoria, peso_unitario_g, stock_disponible, stock_minimo
    ("ARD-UNO", "Arduino Uno R3", "COMPONENTE", 25.0, 12, 3),
    ("KIT-RES", "Kit de resistencias 1/4W (600 uds)", "COMPONENTE", 90.0, 8, 2),
    ("PROTO-830", "Protoboard 830 puntos", "COMPONENTE", 60.0, 15, 4),
    ("CAUTIN-30", "Cautín 30W", "HERRAMIENTA", 130.0, 6, 2),
    ("MULTI-DT830", "Multímetro digital DT830", "HERRAMIENTA", 350.0, 5, 2),
    ("ISO-500", "Alcohol isopropílico 500 ml", "INSUMO_LAB", 450.0, 10, 3),
]

CASILLEROS_DEMO = ["A1", "A2", "A3", "A4", "B1", "B2"]


def conectar(ruta=RUTA_BD) -> sqlite3.Connection:
    """Abre una conexión con claves foráneas activas y filas accesibles por nombre."""
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")  # SQLite lo trae apagado por defecto
    return con


def crear_esquema(con: sqlite3.Connection) -> None:
    con.executescript(RUTA_ESQUEMA.read_text(encoding="utf-8"))


def cargar_datos_demo(con: sqlite3.Connection) -> None:
    con.executemany(
        "INSERT INTO productos (sku, nombre, categoria, peso_unitario_g,"
        " stock_disponible, stock_minimo) VALUES (?, ?, ?, ?, ?, ?)",
        PRODUCTOS_DEMO,
    )
    con.executemany(
        "INSERT INTO casilleros (codigo) VALUES (?)",
        [(codigo,) for codigo in CASILLEROS_DEMO],
    )
    con.commit()


def inicializar(ruta=RUTA_BD, demo: bool = True) -> sqlite3.Connection:
    """Crea la BD desde cero (borra la anterior si existe) y devuelve la conexión."""
    if ruta != ":memory:":
        Path(ruta).unlink(missing_ok=True)
    con = conectar(ruta)
    crear_esquema(con)
    if demo:
        cargar_datos_demo(con)
    return con


if __name__ == "__main__":
    con = inicializar()
    n_prod = con.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    n_cas = con.execute("SELECT COUNT(*) FROM casilleros").fetchone()[0]
    print(f"BD creada en {RUTA_BD.name}: {n_prod} productos, {n_cas} casilleros")
