"""API REST del dispensador (FastAPI). No tiene reglas de negocio: llama a servicio.py.

Perfiles (sin autenticación en este MVP; se separan por ruta y por etiqueta en /docs):
  * Cliente        -> /productos, /pedidos...
  * Máquina        -> /maquina/...  (lo que enviarían la celda de carga y la cámara simuladas)
  * Administrador  -> /admin/...
"""
from datetime import timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import cuentas
import db
import servicio


class ItemIn(BaseModel):
    sku: str = Field(examples=["ARD-UNO"])
    cantidad: int = Field(gt=0, examples=[2])


class PedidoIn(BaseModel):
    # El nombre y el contacto salen de la cuenta con la que el cliente inició sesión.
    items: list[ItemIn] = Field(min_length=1)


class RegistroIn(BaseModel):
    correo: str = Field(max_length=120, examples=["ana@correo.co"])
    usuario: str = Field(max_length=30, examples=["ana_g"])
    contrasena: str = Field(max_length=128, examples=["una-clave-segura"])


class LoginIn(BaseModel):
    usuario: str = Field(max_length=120, description="Usuario o correo", examples=["ana_g"])
    contrasena: str = Field(max_length=128, examples=["una-clave-segura"])


class VerificacionIn(BaseModel):
    peso_medido_g: float = Field(ge=0, examples=[110.0])
    foto_ruta: str = Field(pattern=r"(?i)^.+\.jpe?g$", examples=["fotos/pedido1.jpg"])



class AbrirIn(BaseModel):
     pin: str = Field(pattern=r"^\d{1,12}$", examples=["123456"])

class DespacharIn(BaseModel):
    # Solo para pruebas: sensor que «no detecta» la caja, para simular un atasco (P4).
    atasco_en: Literal["S_ENTRADA", "S_DESVIO", "S_CASILLERO"] | None = Field(
        default=None, examples=[None])


class ReabastecerIn(BaseModel):
    cantidad: int = Field(gt=0, examples=[10])


class AdelantarIn(BaseModel):
    horas: float = Field(gt=0, le=24 * 30, examples=[24])


CARPETA_PANTALLAS = Path(__file__).resolve().parent / "pantallas"


def crear_app(ruta_bd=db.RUTA_BD, reloj=None) -> FastAPI:
    """`reloj` es una función sin argumentos que devuelve la hora UTC; las pruebas pasan
    uno falso para simular que transcurren 24 horas."""
    if str(ruta_bd) == ":memory:":
        raise ValueError("La API necesita una BD en archivo (cada petición abre su conexión).")
    if not Path(ruta_bd).exists():
        db.inicializar(ruta_bd).close()

    bearer = HTTPBearer(auto_error=False)

    def conexion(request: Request):
        con = db.conectar(request.app.state.ruta_bd, multihilo=True)
        try:
            yield con
        finally:
            con.close()

    def ahora(request: Request):
        return request.app.state.reloj() + request.app.state.desfase

    def token_de(cred: HTTPAuthorizationCredentials | None = Depends(bearer)):
        return cred.credentials if cred else None

    def usuario_actual(token=Depends(token_de), con=Depends(conexion), ahora=Depends(ahora)):
        return cuentas.usuario_por_token(con, token, ahora)

    def proteger(request: Request, token=Depends(token_de), con=Depends(conexion),
                 ahora=Depends(ahora)):
        """Toda ruta /admin/... y /maquina/... exige una sesión de administrador."""
        ruta = request.url.path
        if ruta.startswith("/admin/") or ruta.startswith("/maquina/"):
            if cuentas.usuario_por_token(con, token, ahora)["rol"] != "ADMIN":
                raise cuentas.SinPermiso("Esta acción es solo para administradores.")

    def ver_pedido_propio(pedido_id: int, usuario=Depends(usuario_actual), con=Depends(conexion)):
        """El cliente solo ve sus pedidos; el administrador ve todos."""
        if usuario["rol"] != "ADMIN" and servicio.dueno_de_pedido(con, pedido_id) != usuario["id"]:
            raise servicio.NoEncontrado(f"No existe el pedido {pedido_id}.")
        return pedido_id

    app = FastAPI(title="Dispensador automatizado - MVP", version="0.3",
                  dependencies=[Depends(proteger)])
    app.state.ruta_bd = ruta_bd
    app.state.reloj = reloj or servicio.ahora_utc
    app.state.desfase = timedelta(0)  # reloj simulado: el admin puede "adelantar" el tiempo
    app.mount("/pantallas", StaticFiles(directory=CARPETA_PANTALLAS), name="pantallas")

    @app.exception_handler(servicio.ErrorNegocio)
    async def _error_negocio(_, exc: servicio.ErrorNegocio):
        return JSONResponse({"error": exc.codigo, "detalle": exc.mensaje}, exc.status)

    # ------------------------------------------------------------ pantallas
    @app.get("/", include_in_schema=False)
    def inicio():
        return FileResponse(CARPETA_PANTALLAS / "index.html")

    @app.get("/cliente", include_in_schema=False)
    def pantalla_cliente():
        return FileResponse(CARPETA_PANTALLAS / "cliente.html")

    @app.get("/admin", include_in_schema=False)
    def pantalla_admin():
        return FileResponse(CARPETA_PANTALLAS / "admin.html")

    # ------------------------------------------------------------ cuentas
    @app.post("/cuentas/registro", status_code=201, tags=["Cuentas"],
              summary="Crear cuenta de cliente (correo, usuario y contraseña) e iniciar sesión")
    def registro(datos: RegistroIn, con=Depends(conexion), ahora=Depends(ahora)):
        cuentas.crear_usuario(con, datos.correo, datos.usuario, datos.contrasena, "CLIENTE", ahora)
        return cuentas.iniciar_sesion(con, datos.usuario, datos.contrasena, ahora)

    @app.post("/cuentas/login", tags=["Cuentas"], summary="Iniciar sesión (cliente o administrador)")
    def login(datos: LoginIn, con=Depends(conexion), ahora=Depends(ahora)):
        return cuentas.iniciar_sesion(con, datos.usuario, datos.contrasena, ahora)

    @app.post("/cuentas/logout", tags=["Cuentas"], summary="Cerrar sesión")
    def logout(token=Depends(token_de), con=Depends(conexion)):
        cuentas.cerrar_sesion(con, token)
        return {"sesion_cerrada": True}

    @app.get("/cuentas/yo", tags=["Cuentas"], summary="Datos de la cuenta con sesión iniciada")
    def yo(usuario=Depends(usuario_actual)):
        return usuario

    # ------------------------------------------------------------ cliente
    @app.get("/productos", tags=["Cliente"], summary="Disponibilidad de productos")
    def productos(con=Depends(conexion)):
        return servicio.inventario(con)

    @app.post("/pedidos", status_code=201, tags=["Cliente"],
              summary="Crear pedido (carrito). Se asigna casillero o entra en cola")
    def crear_pedido(datos: PedidoIn, usuario=Depends(usuario_actual), con=Depends(conexion),
                     ahora=Depends(ahora)):
        if usuario["rol"] != "CLIENTE":
            raise cuentas.SinPermiso("Los pedidos se hacen con una cuenta de cliente.")
        return servicio.crear_pedido(
            con, usuario["usuario"], usuario["correo"],
            [(i.sku, i.cantidad) for i in datos.items], ahora, usuario_id=usuario["id"])

    @app.get("/mis-pedidos", tags=["Cliente"], summary="Pedidos de la cuenta con sesión iniciada")
    def mis_pedidos(usuario=Depends(usuario_actual), con=Depends(conexion)):
        return servicio.listar_pedidos_de_usuario(con, usuario["id"])

    @app.get("/pedidos/{pedido_id}", tags=["Cliente"], summary="Estado de un pedido")
    def ver_pedido(pedido_id: int = Depends(ver_pedido_propio), con=Depends(conexion)):
        return servicio.obtener_pedido(con, pedido_id)

    @app.get("/pedidos/{pedido_id}/notificaciones", tags=["Cliente"],
             summary="Mensajes enviados al cliente (aquí llega el PIN)")
    def notificaciones(pedido_id: int = Depends(ver_pedido_propio), con=Depends(conexion)):
        return servicio.listar_notificaciones(con, pedido_id)

    @app.get("/pedidos/{pedido_id}/trazabilidad", tags=["Cliente"],
             summary="Historial completo del pedido, de la reserva a la entrega")
    def trazabilidad(pedido_id: int = Depends(ver_pedido_propio), con=Depends(conexion)):
        return servicio.listar_bitacora(con, pedido_id)

    @app.post("/pedidos/{pedido_id}/cancelar", tags=["Cliente"],
              summary="Cancelar (solo antes de que la banda mueva la caja)")
    def cancelar(pedido_id: int = Depends(ver_pedido_propio), con=Depends(conexion),
                 ahora=Depends(ahora)):
        return servicio.cancelar_pedido(con, pedido_id, ahora)

    @app.get("/casilleros", tags=["Cliente"], summary="Códigos de los casilleros")
    def casilleros(con=Depends(conexion)):
        return servicio.listar_codigos_casilleros(con)

    @app.post("/casilleros/{codigo}/abrir", tags=["Cliente"],
              summary="Digitar el PIN en el casillero")
    def abrir(codigo: str, datos: AbrirIn, con=Depends(conexion), ahora=Depends(ahora)):
        resultado = servicio.abrir_casillero(con, codigo, datos.pin, ahora)
        return JSONResponse(resultado, 200 if resultado["abierto"] else 403)

    # ------------------------------------------------------------ máquina (simulada)
    @app.post("/maquina/pedidos/{pedido_id}/despachar", tags=["Máquina (simulada)"],
              summary="Llevar la caja por la banda hasta el casillero (máquina de estados). "
                      "También sirve para reintentar tras un atasco")
    def despachar(pedido_id: int, datos: DespacharIn | None = None, con=Depends(conexion),
                  ahora=Depends(ahora)):
        return servicio.despachar_pedido(con, pedido_id, datos.atasco_en if datos else None, ahora)

    @app.post("/maquina/pedidos/{pedido_id}/verificacion", tags=["Máquina (simulada)"],
              summary="Enviar peso medido (celda de carga) y foto (.jpg) del casillero")
    def verificacion(pedido_id: int, datos: VerificacionIn, con=Depends(conexion),
                     ahora=Depends(ahora)):
        return servicio.registrar_verificacion(
            con, pedido_id, datos.peso_medido_g, datos.foto_ruta, ahora)

    # ------------------------------------------------------------ administrador
    @app.get("/admin/inventario", tags=["Administrador"], summary="Inventario en tiempo real")
    def admin_inventario(con=Depends(conexion)):
        return servicio.inventario(con)

    @app.get("/admin/pedidos", tags=["Administrador"], summary="Pedidos recientes")
    def admin_pedidos(con=Depends(conexion)):
        return servicio.listar_pedidos(con)

    @app.get("/admin/reloj", tags=["Administrador"], summary="Hora del sistema (simulada)")
    def ver_reloj(request: Request, ahora=Depends(ahora)):
        return {"ahora": servicio.fmt(ahora),
                "adelanto_horas": request.app.state.desfase.total_seconds() / 3600}

    @app.post("/admin/reloj/adelantar", tags=["Administrador"],
              summary="[Simulación] Adelantar el reloj para probar la caducidad")
    def adelantar_reloj(datos: AdelantarIn, request: Request):
        request.app.state.desfase += timedelta(hours=datos.horas)
        return ver_reloj(request, ahora(request))

    @app.post("/admin/productos/{sku}/reabastecer", tags=["Administrador"],
              summary="Sumar unidades al inventario")
    def reabastecer(sku: str, datos: ReabastecerIn, con=Depends(conexion), ahora=Depends(ahora)):
        return servicio.reabastecer(con, sku, datos.cantidad, ahora)

    @app.get("/admin/casilleros", tags=["Administrador"],
             summary="Estado y % de ocupación de los casilleros")
    def admin_casilleros(con=Depends(conexion)):
        return servicio.estado_casilleros(con)

    @app.post("/admin/casilleros/{codigo}/mantenimiento", tags=["Administrador"],
              summary="Sacar un casillero libre de servicio")
    def mantenimiento_on(codigo: str, con=Depends(conexion), ahora=Depends(ahora)):
        return servicio.cambiar_mantenimiento(con, codigo, True, ahora)

    @app.post("/admin/casilleros/{codigo}/liberar", tags=["Administrador"],
              summary="Devolver un casillero a servicio (y repartir la cola)")
    def mantenimiento_off(codigo: str, con=Depends(conexion), ahora=Depends(ahora)):
        return servicio.cambiar_mantenimiento(con, codigo, False, ahora)

    @app.get("/admin/alertas", tags=["Administrador"], summary="Alertas del sistema")
    def alertas(solo_pendientes: bool = True, con=Depends(conexion)):
        return servicio.listar_alertas(con, solo_pendientes)

    @app.post("/admin/alertas/{alerta_id}/atender", tags=["Administrador"],
              summary="Marcar una alerta como atendida")
    def atender(alerta_id: int, con=Depends(conexion), ahora=Depends(ahora)):
        servicio.atender_alerta(con, alerta_id, ahora)
        return {"atendida": True}

    @app.post("/admin/caducar-vencidos", tags=["Administrador"],
              summary="Caducar PIN vencidos (24 h) y reasignar casilleros a la cola")
    def caducar(con=Depends(conexion), ahora=Depends(ahora)):
        return {"pedidos_caducados": servicio.caducar_vencidos(con, ahora)}

    @app.get("/admin/maquina", tags=["Administrador"],
             summary="Estado de la banda y últimas señales de sensores y actuadores")
    def admin_maquina(con=Depends(conexion)):
        return servicio.estado_maquina(con)

    @app.post("/admin/pedidos/{pedido_id}/cancelar", tags=["Administrador"],
              summary="Cancelar un pedido (incluye pedidos ATASCADOS)")
    def admin_cancelar(pedido_id: int, con=Depends(conexion), ahora=Depends(ahora)):
        return servicio.cancelar_pedido(con, pedido_id, ahora, por_admin=True)

    @app.post("/admin/pedidos/{pedido_id}/reemitir-pin", tags=["Administrador"],
              summary="Nuevo PIN cuando el anterior quedó bloqueado")
    def reemitir(pedido_id: int, con=Depends(conexion), ahora=Depends(ahora)):
        return servicio.reemitir_pin(con, pedido_id, ahora)

    return app


app = crear_app()
