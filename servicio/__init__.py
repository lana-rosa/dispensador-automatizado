"""Lógica de negocio del dispensador.

Aquí viven TODAS las reglas (stock, pago, cola, PIN, peso, caducidad). La API solo traduce
HTTP <-> estas funciones, y la simulación de la máquina las reutiliza.

El paquete se divide por responsabilidad (cada módulo solo importa de los anteriores):

  nucleo        errores de negocio, transiciones de estado, transacciones y ayudas internas
  alertas       alertas del administrador
  inventario    stock disponible y reservado, reabastecimiento
  casilleros    asignación, cola FIFO, mantenimiento y ocupación
  pedidos       crear el carrito, pagar (descuenta stock), consultar y cancelar
  banda         banda transportadora simulada: despacho, atascos y estado de la máquina
  retiro        PIN, apertura del casillero, caducidad a las 24 h y reemisión del PIN
  verificacion  peso medido y foto simulada, falla de la báscula

Para quien llama, nada cambió: se sigue usando `servicio.crear_pedido(...)`, `servicio.ErrorNegocio`,
etc., porque este archivo vuelve a exportar los nombres públicos de cada módulo.

Convenciones:
  * Cada función pública recibe `ahora` (datetime UTC) para poder simular el paso del
    tiempo en las pruebas (P5: "pasaron 24 h") sin esperar.
  * Cada función pública que escribe corre dentro de UNA transacción: o se guarda todo
    o no se guarda nada. Las funciones que empiezan con "_" son internas y no abren
    transacción propia.
"""
from .nucleo import (
    FORMATO_FECHA, TRANSICIONES, ErrorNegocio, NoEncontrado, SinStock, EstadoInvalido,
    SolicitudInvalida, ahora_utc, fmt, transaccion, _config,
)  # noqa: F401
from .alertas import (
    listar_alertas, atender_alerta,
)  # noqa: F401
from .inventario import (
    inventario, reabastecer,
)  # noqa: F401
from .casilleros import (
    estado_casilleros, cambiar_mantenimiento, listar_codigos_casilleros,
)  # noqa: F401
from .pedidos import (
    crear_pedido, pagar_pedido, obtener_pedido, listar_notificaciones, listar_bitacora,
    cancelar_pedido, listar_pedidos, listar_pedidos_de_usuario, dueno_de_pedido,
)  # noqa: F401
from .banda import (
    despachar_pedido, estado_maquina, pedido_atascado,
)  # noqa: F401
from .retiro import (
    abrir_casillero, caducar_vencidos, reemitir_pin,
)  # noqa: F401
from .verificacion import (
    registrar_verificacion, resolver_error_despacho,
)  # noqa: F401
