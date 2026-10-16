
"""API REST del dispensador automatizado (FastAPI).
Incluye control simulado de la banda transportadora, llegada,
detección de atascos y apertura de casilleros.
"""

from datetime import timedelta
from pathlib import Path
import time

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import servicio


# ------------------------------------------------------------
# Modelos de entrada
# ------------------------------------------------------------

class ItemIn(BaseModel):
    sku: str = Field(examples=["ARD-UNO"])
    cantidad: int = Field(gt=0, examples=[2])


class PedidoIn(BaseModel):
    cliente_nombre: str = Field(min_length=1, examples=["Ana Gómez"])
    cliente_contacto: str = Field(min_length=3, examples=["ana@correo.co"])
    items: list[ItemIn] = Field(min_length=1)


class VerificacionIn(BaseModel):
    peso_medido_g: float = Field(ge=0, examples=[110.0])
    foto_ruta: str = Field(
        pattern=r"(?i)^.+\.jpe?g$",
        examples=["fotos/pedido1.jpg"]
    )


class AbrirIn(BaseModel):
    pin: str = Field(
        pattern=r"^\d{6}$",
        examples=["123456"],
        description="PIN de exactamente 6 dígitos"
    )


class ReabastecerIn(BaseModel):
    cantidad: int = Field(gt=0, examples=[10])


class AdelantarIn(BaseModel):
    horas: float = Field(gt=0, le=24 * 30, examples=[24])


# ------------------------------------------------------------
# Configuración
# ------------------------------------------------------------

CARPETA_PANTALLAS = Path(__file__).resolve().parent / "pantallas"


def crear_app(ruta_bd=db.RUTA_BD, reloj=None) -> FastAPI:
    """Crea la aplicación y permite simular el reloj en las pruebas."""

    if str(ruta_bd) == ":memory:":
        raise ValueError(
            "La API necesita una BD en archivo "
            "(cada petición abre su conexión)."
        )

    if not Path(ruta_bd).exists():
        db.inicializar(ruta_bd).close()

    app = FastAPI(
        title="Dispensador automatizado - MVP",
        version="0.3"
    )

    app.state.ruta_bd = ruta_bd
    app.state.reloj = reloj or servicio.ahora_utc
    app.state.desfase = timedelta(0)

    # Estado de la banda transportadora simulada.
    app.state.banda = {
        "estado": "DETENIDA",
        "pedido_id": None,
        "inicio_movimiento": None,
        "tiempo_maximo_s": 15
    }

    app.mount(
        "/pantallas",
        StaticFiles(directory=CARPETA_PANTALLAS),
        name="pantallas"
    )

    # --------------------------------------------------------
    # Dependencias
    # --------------------------------------------------------

    def conexion(request: Request):
        con = db.conectar(
            request.app.state.ruta_bd,
            multihilo=True
        )
        try:
            yield con
        finally:
            con.close()

    def ahora(request: Request):
        return (
            request.app.state.reloj()
            + request.app.state.desfase
        )

    # --------------------------------------------------------
    # Manejo de errores de negocio
    # --------------------------------------------------------

    @app.exception_handler(servicio.ErrorNegocio)
    async def _error_negocio(_, exc: servicio.ErrorNegocio):
        return JSONResponse(
            {"error": exc.codigo, "detalle": exc.mensaje},
            status_code=exc.status
        )

    # --------------------------------------------------------
    # Pantallas
    # --------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def inicio():
        return FileResponse(CARPETA_PANTALLAS / "index.html")

    @app.get("/cliente", include_in_schema=False)
    def pantalla_cliente():
        return FileResponse(CARPETA_PANTALLAS / "cliente.html")

    @app.get("/admin", include_in_schema=False)
    def pantalla_admin():
        return FileResponse(CARPETA_PANTALLAS / "admin.html")

    # --------------------------------------------------------
    # Cliente
    # --------------------------------------------------------

    @app.get(
        "/productos",
        tags=["Cliente"],
        summary="Disponibilidad de productos"
    )
    def productos(con=Depends(conexion)):
        return servicio.inventario(con)

    @app.post(
        "/pedidos",
        status_code=201,
        tags=["Cliente"],
        summary="Crear pedido"
    )
    def crear_pedido(
        datos: PedidoIn,
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        return servicio.crear_pedido(
            con,
            datos.cliente_nombre,
            datos.cliente_contacto,
            [(i.sku, i.cantidad) for i in datos.items],
            ahora
        )

    @app.get(
        "/pedidos/{pedido_id}",
        tags=["Cliente"],
        summary="Estado de un pedido"
    )
    def ver_pedido(pedido_id: int, con=Depends(conexion)):
        return servicio.obtener_pedido(con, pedido_id)

    @app.get(
        "/pedidos/{pedido_id}/notificaciones",
        tags=["Cliente"],
        summary="Notificaciones del pedido"
    )
    def notificaciones(pedido_id: int, con=Depends(conexion)):
        return servicio.listar_notificaciones(con, pedido_id)

    @app.get(
        "/pedidos/{pedido_id}/trazabilidad",
        tags=["Cliente"],
        summary="Historial completo del pedido"
    )
    def trazabilidad(pedido_id: int, con=Depends(conexion)):
        return servicio.listar_bitacora(con, pedido_id)

    @app.post(
        "/pedidos/{pedido_id}/cancelar",
        tags=["Cliente"],
        summary="Cancelar un pedido"
    )
    def cancelar(
        pedido_id: int,
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        return servicio.cancelar_pedido(con, pedido_id, ahora)

    @app.get(
        "/casilleros",
        tags=["Cliente"],
        summary="Códigos de los casilleros"
    )
    def casilleros(con=Depends(conexion)):
        return servicio.listar_codigos_casilleros(con)

    @app.post(
        "/casilleros/{codigo}/abrir",
        tags=["Cliente"],
        summary="Abrir casillero con PIN"
    )
    def abrir(
        codigo: str,
        datos: AbrirIn,
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        resultado = servicio.abrir_casillero(
            con, codigo, datos.pin, ahora
        )

        return JSONResponse(
            resultado,
            status_code=200 if resultado["abierto"] else 403
        )

    # --------------------------------------------------------
    # Máquina y banda transportadora (simuladas)
    # --------------------------------------------------------

    @app.get(
        "/maquina/banda/estado",
        tags=["Máquina (simulada)"],
        summary="Consultar estado de la banda"
    )
    def estado_banda(request: Request):
        banda = request.app.state.banda

        # Detectar exceso de tiempo durante una consulta.
        if banda["estado"] == "EN_MOVIMIENTO":
            inicio = banda["inicio_movimiento"]

            if inicio is not None:
                transcurrido = time.monotonic() - inicio

                if transcurrido > banda["tiempo_maximo_s"]:
                    banda["estado"] = "ATASCO"
                    banda["inicio_movimiento"] = None

        return {
            "estado": banda["estado"],
            "pedido_id": banda["pedido_id"],
            "tiempo_maximo_s": banda["tiempo_maximo_s"]
        }

    @app.post(
        "/maquina/banda/iniciar/{pedido_id}",
        tags=["Máquina (simulada)"],
        summary="Iniciar transporte de un pedido"
    )
    def iniciar_banda(pedido_id: int, request: Request):
        banda = request.app.state.banda

        if banda["estado"] == "ATASCO":
            return JSONResponse(
                {"error": "Banda bloqueada. Requiere revisión."},
                status_code=409
            )

        if banda["estado"] == "EN_MOVIMIENTO":
            return JSONResponse(
                {"error": "La banda ya está en movimiento."},
                status_code=409
            )

        banda["estado"] = "EN_MOVIMIENTO"
        banda["pedido_id"] = pedido_id
        banda["inicio_movimiento"] = time.monotonic()

        return {
            "mensaje": "Transporte iniciado",
            "pedido_id": pedido_id,
            "estado": banda["estado"]
        }

    @app.post(
        "/maquina/banda/atasco",
        tags=["Máquina (simulada)"],
        summary="Simular detección de atasco"
    )
    def detectar_atasco(request: Request):
        banda = request.app.state.banda

        banda["estado"] = "ATASCO"
        banda["inicio_movimiento"] = None

        return {
            "mensaje": "Atasco detectado. Banda detenida.",
            "pedido_id": banda["pedido_id"],
            "estado": banda["estado"]
        }

    @app.post(
        "/maquina/banda/reiniciar",
        tags=["Máquina (simulada)"],
        summary="Restablecer banda tras revisar el atasco"
    )
    def reiniciar_banda(request: Request):
        banda = request.app.state.banda

        if banda["estado"] != "ATASCO":
            return JSONResponse(
                {"error": "La banda no está en estado de atasco."},
                status_code=409
            )

        banda["estado"] = "DETENIDA"
        banda["pedido_id"] = None
        banda["inicio_movimiento"] = None

        return {
            "mensaje": "Banda restablecida",
            "estado": banda["estado"]
        }

    @app.post(
        "/maquina/pedidos/{pedido_id}/llegada",
        tags=["Máquina (simulada)"],
        summary="Registrar llegada de la caja al casillero"
    )
    def llegada(
        pedido_id: int,
        request: Request,
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        banda = request.app.state.banda

        # No aceptar llegada si la banda está atascada.
        if banda["estado"] == "ATASCO":
            return JSONResponse(
                {"error": "No se puede registrar la llegada: banda atascada."},
                status_code=409
            )

        # Si hay un pedido en movimiento, debe coincidir.
        if (
            banda["estado"] == "EN_MOVIMIENTO"
            and banda["pedido_id"] != pedido_id
        ):
            return JSONResponse(
                {"error": "El pedido no coincide con el transporte actual."},
                status_code=409
            )

        # Ejecutar la lógica de negocio original.
        resultado = servicio.simular_llegada_a_casillero(
            con, pedido_id, ahora
        )

        # Detener la banda después de registrar la llegada.
        if banda["pedido_id"] == pedido_id:
            banda["estado"] = "DETENIDA"
            banda["pedido_id"] = None
            banda["inicio_movimiento"] = None

        return resultado

    @app.post(
        "/maquina/pedidos/{pedido_id}/verificacion",
        tags=["Máquina (simulada)"],
        summary="Enviar peso y ruta de foto para verificar el pedido"
    )
    def verificacion(
        pedido_id: int,
        datos: VerificacionIn,
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        return servicio.registrar_verificacion(
            con,
            pedido_id,
            datos.peso_medido_g,
            datos.foto_ruta,
            ahora
        )

    # --------------------------------------------------------
    # Administrador
    # --------------------------------------------------------

    @app.get(
        "/admin/inventario",
        tags=["Administrador"],
        summary="Inventario en tiempo real"
    )
    def admin_inventario(con=Depends(conexion)):
        return servicio.inventario(con)

    @app.get(
        "/admin/pedidos",
        tags=["Administrador"],
        summary="Pedidos recientes"
    )
    def admin_pedidos(con=Depends(conexion)):
        return servicio.listar_pedidos(con)

    @app.get(
        "/admin/reloj",
        tags=["Administrador"],
        summary="Hora del sistema simulada"
    )
    def ver_reloj(request: Request, ahora=Depends(ahora)):
        return {
            "ahora": servicio.fmt(ahora),
            "adelanto_horas": (
                request.app.state.desfase.total_seconds() / 3600
            )
        }

    @app.post(
        "/admin/reloj/adelantar",
        tags=["Administrador"],
        summary="Adelantar el reloj para probar la caducidad"
    )
    def adelantar_reloj(datos: AdelantarIn, request: Request):
        request.app.state.desfase += timedelta(hours=datos.horas)

        return ver_reloj(request, ahora(request))

    @app.post(
        "/admin/productos/{sku}/reabastecer",
        tags=["Administrador"],
        summary="Sumar unidades al inventario"
    )
    def reabastecer(
        sku: str,
        datos: ReabastecerIn,
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        return servicio.reabastecer(
            con, sku, datos.cantidad, ahora
        )

    @app.get(
        "/admin/casilleros",
        tags=["Administrador"],
        summary="Estado y porcentaje de ocupación de casilleros"
    )
    def admin_casilleros(con=Depends(conexion)):
        return servicio.estado_casilleros(con)

    @app.post(
        "/admin/casilleros/{codigo}/mantenimiento",
        tags=["Administrador"],
        summary="Sacar un casillero libre de servicio"
    )
    def mantenimiento_on(
        codigo: str,
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        return servicio.cambiar_mantenimiento(
            con, codigo, True, ahora
        )

    @app.post(
        "/admin/casilleros/{codigo}/liberar",
        tags=["Administrador"],
        summary="Devolver un casillero a servicio"
    )
    def mantenimiento_off(
        codigo: str,
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        return servicio.cambiar_mantenimiento(
            con, codigo, False, ahora
        )

    @app.get(
        "/admin/alertas",
        tags=["Administrador"],
        summary="Alertas del sistema"
    )
    def alertas(
        solo_pendientes: bool = True,
        con=Depends(conexion)
    ):
        return servicio.listar_alertas(con, solo_pendientes)

    @app.post(
        "/admin/alertas/{alerta_id}/atender",
        tags=["Administrador"],
        summary="Marcar alerta como atendida"
    )
    def atender(
        alerta_id: int,
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        servicio.atender_alerta(con, alerta_id, ahora)
        return {"atendida": True}

    @app.post(
        "/admin/caducar-vencidos",
        tags=["Administrador"],
        summary="Caducar PIN vencidos y reasignar casilleros"
    )
    def caducar(
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        return {
            "pedidos_caducados": servicio.caducar_vencidos(
                con, ahora
            )
        }

    @app.post(
        "/admin/pedidos/{pedido_id}/reemitir-pin",
        tags=["Administrador"],
        summary="Generar un nuevo PIN"
    )
    def reemitir(
        pedido_id: int,
        con=Depends(conexion),
        ahora=Depends(ahora)
    ):
        return servicio.reemitir_pin(con, pedido_id, ahora)

    return app


app = crear_app()
