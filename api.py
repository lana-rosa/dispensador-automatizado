"""API REST del dispensador (FastAPI). No tiene reglas de negocio: llama a servicio.py.

Perfiles (sin autenticación en este MVP; se separan por ruta y por etiqueta en /docs):
  * Cliente        -> /productos, /pedidos...
  * Máquina        -> /maquina/...  (lo que enviarían la celda de carga y la cámara simuladas)
  * Administrador  -> /admin/...
"""
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import servicio


class ItemIn(BaseModel):
    sku: str = Field(examples=["ARD-UNO"])
    cantidad: int = Field(gt=0, examples=[2])


class PedidoIn(BaseModel):
    cliente_nombre: str = Field(min_length=1, examples=["Ana Gómez"])
    cliente_contacto: str = Field(min_length=3, examples=["ana@correo.co"])
    items: list[ItemIn] = Field(min_length=1)


class VerificacionIn(BaseModel):
    peso_medido_g: float = Field(ge=0, examples=[110.0])
    foto_ruta: str = Field(pattern=r"(?i)^.+\.jpe?g$", examples=["fotos/pedido1.jpg"])


class AbrirIn(BaseModel):
    pin: str = Field(pattern=r"^\d{1,12}$", examples=["123456"])


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

    app = FastAPI(title="Dispensador automatizado - MVP", version="0.2")
    app.state.ruta_bd = ruta_bd
    app.state.reloj = reloj or servicio.ahora_utc
    app.state.desfase = timedelta(0)  # reloj simulado: el admin puede "adelantar" el tiempo
    app.mount("/pantallas", StaticFiles(directory=CARPETA_PANTALLAS), name="pantallas")

    def conexion(request: Request):
        con = db.conectar(request.app.state.ruta_bd, multihilo=True)
        try:
            yield con
        finally:
            con.close()

    def ahora(request: Request):
        return request.app.state.reloj() + request.app.state.desfase

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

    # ------------------------------------------------------------ cliente
    @app.get("/productos", tags=["Cliente"], summary="Disponibilidad de productos")
    def productos(con=Depends(conexion)):
        return servicio.inventario(con)

    @app.post("/pedidos", status_code=201, tags=["Cliente"],
              summary="Crear pedido (carrito). Se asigna casillero o entra en cola")
    def crear_pedido(datos: PedidoIn, con=Depends(conexion), ahora=Depends(ahora)):
        return servicio.crear_pedido(
            con, datos.cliente_nombre, datos.cliente_contacto,
            [(i.sku, i.cantidad) for i in datos.items], ahora)

    @app.get("/pedidos/{pedido_id}", tags=["Cliente"], summary="Estado de un pedido")
    def ver_pedido(pedido_id: int, con=Depends(conexion)):
        return servicio.obtener_pedido(con, pedido_id)

    @app.get("/pedidos/{pedido_id}/notificaciones", tags=["Cliente"],
             summary="Mensajes enviados al cliente (aquí llega el PIN)")
    def notificaciones(pedido_id: int, con=Depends(conexion)):
        return servicio.listar_notificaciones(con, pedido_id)

    @app.get("/pedidos/{pedido_id}/trazabilidad", tags=["Cliente"],
             summary="Historial completo del pedido, de la reserva a la entrega")
    def trazabilidad(pedido_id: int, con=Depends(conexion)):
        return servicio.listar_bitacora(con, pedido_id)

    @app.post("/pedidos/{pedido_id}/cancelar", tags=["Cliente"],
              summary="Cancelar (solo antes de que la banda mueva la caja)")
    def cancelar(pedido_id: int, con=Depends(conexion), ahora=Depends(ahora)):
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
    @app.post("/maquina/pedidos/{pedido_id}/llegada", tags=["Máquina (simulada)"],
              summary="[Provisional] Simula que la banda dejó la caja en el casillero")
    def llegada(pedido_id: int, con=Depends(conexion), ahora=Depends(ahora)):
        return servicio.simular_llegada_a_casillero(con, pedido_id, ahora)

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

    @app.post("/admin/pedidos/{pedido_id}/reemitir-pin", tags=["Administrador"],
              summary="Nuevo PIN cuando el anterior quedó bloqueado")
    def reemitir(pedido_id: int, con=Depends(conexion), ahora=Depends(ahora)):
        return servicio.reemitir_pin(con, pedido_id, ahora)

    return app


app = crear_app()
