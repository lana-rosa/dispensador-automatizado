"""Máquinas de estados finitos del hardware simulado (Issue 2).

Componentes simulados
  * Motor de la banda transportadora ........ salida  MOTOR_BANDA=ON/OFF
  * Sensores fotoeléctricos ................. entradas S_ENTRADA, S_DESVIO, S_CASILLERO
  * Actuador de desvío ...................... salida  ACTUADOR_DESVIO=ON/OFF
  * Electrocerradura de cada casillero ...... salida  ELECTROCERRADURA_<cod>=ON/OFF
  * Sensor de puerta de cada casillero ...... entrada PUERTA_<cod>=ABIERTA/CERRADA

Este módulo NO toca la base de datos: solo calcula la secuencia de eventos. el paquete servicio/ la
guarda en la bitácora y actualiza el pedido. Así la lógica de control se puede probar sola.

Tiempo simulado: no se esperan segundos reales; cada evento lleva el segundo «t» en que
ocurriría. Por eso la regla «si el sensor no detecta avance tras X s → detener y alertar»
se verifica al instante en las pruebas.
"""
from dataclasses import dataclass, field
from enum import Enum


class EstadoBanda(str, Enum):
    REPOSO = "REPOSO"                        # motor apagado, sin caja
    ESPERANDO_ENTRADA = "ESPERANDO_ENTRADA"  # motor encendido, la caja aún no cruza S_ENTRADA
    TRANSPORTANDO = "TRANSPORTANDO"          # la caja avanza hacia el desvío
    DESVIANDO = "DESVIANDO"                  # actuador empuja la caja hacia el casillero
    ENTREGADO = "ENTREGADO"                  # caja dentro del casillero, motor apagado
    DETENIDA_ATASCO = "DETENIDA_ATASCO"      # un sensor no respondió a tiempo: motores apagados


class EstadoCerradura(str, Enum):
    BLOQUEADA = "BLOQUEADA"
    ABIERTA = "ABIERTA"


SENSORES = ("S_ENTRADA", "S_DESVIO", "S_CASILLERO")

# En cada estado activo la banda espera UN sensor; cuando se activa, pasa al siguiente estado.
TRANSICIONES_BANDA = {
    EstadoBanda.ESPERANDO_ENTRADA: ("S_ENTRADA", EstadoBanda.TRANSPORTANDO),
    EstadoBanda.TRANSPORTANDO: ("S_DESVIO", EstadoBanda.DESVIANDO),
    EstadoBanda.DESVIANDO: ("S_CASILLERO", EstadoBanda.ENTREGADO),
}

# Segundos que tarda la caja en llegar a cada sensor desde el estado anterior (operación normal).
TIEMPOS_NOMINALES_S = {"S_ENTRADA": 1.0, "S_DESVIO": 4.0, "S_CASILLERO": 1.5}


@dataclass
class Evento:
    t: float      # segundo simulado desde el arranque
    tipo: str     # SENAL (salida a un actuador), SENSOR (entrada), ESTADO, ALARMA
    detalle: str


@dataclass
class Recorrido:
    exito: bool
    estado_final: EstadoBanda
    duracion_s: float
    sensor_fallido: str | None = None
    eventos: list[Evento] = field(default_factory=list)


def simular_recorrido(casillero: str, timeout_s: float, atasco_en: str | None = None,
                      tiempos_s: dict | None = None) -> Recorrido:
    """Lleva una caja por la banda hasta `casillero`.

    atasco_en: nombre de un sensor que nunca se activará (simula una caja trabada antes de él).
    tiempos_s: permite cambiar las demoras nominales; si una demora supera timeout_s, también
               se considera atasco (la caja avanza demasiado lento).
    """
    if atasco_en is not None and atasco_en not in SENSORES:
        raise ValueError(f"Sensor desconocido: {atasco_en}. Opciones: {', '.join(SENSORES)}")
    demoras = {**TIEMPOS_NOMINALES_S, **(tiempos_s or {})}
    eventos: list[Evento] = []
    t = 0.0
    estado = EstadoBanda.REPOSO

    def cambiar(nuevo: EstadoBanda):
        nonlocal estado
        eventos.append(Evento(t, "ESTADO", f"{estado.value} -> {nuevo.value}"))
        estado = nuevo

    eventos.append(Evento(t, "SENAL", "MOTOR_BANDA=ON"))
    cambiar(EstadoBanda.ESPERANDO_ENTRADA)

    while estado in TRANSICIONES_BANDA:
        sensor, siguiente = TRANSICIONES_BANDA[estado]
        demora = demoras[sensor]
        if sensor == atasco_en or demora > timeout_s:
            # Regla de atasco: pasó el tiempo máximo sin detección -> detener todo y alarmar.
            t += timeout_s
            eventos.append(Evento(t, "ALARMA", f"{sensor} sin detección tras {timeout_s:g} s"))
            eventos.append(Evento(t, "SENAL", "MOTOR_BANDA=OFF"))
            if estado == EstadoBanda.DESVIANDO:
                eventos.append(Evento(t, "SENAL", "ACTUADOR_DESVIO=OFF"))
            cambiar(EstadoBanda.DETENIDA_ATASCO)
            return Recorrido(False, estado, t, sensor, eventos)

        t += demora
        eventos.append(Evento(t, "SENSOR", f"{sensor}=1"))
        if siguiente == EstadoBanda.DESVIANDO:
            eventos.append(Evento(t, "SENAL", f"ACTUADOR_DESVIO=ON (casillero {casillero})"))
        if siguiente == EstadoBanda.ENTREGADO:
            eventos.append(Evento(t, "SENAL", "ACTUADOR_DESVIO=OFF"))
            eventos.append(Evento(t, "SENAL", "MOTOR_BANDA=OFF"))
        cambiar(siguiente)

    return Recorrido(True, estado, t, None, eventos)


def ciclo_cerradura(casillero: str, segundos_abierta: float = 8.0) -> list[Evento]:
    """PIN correcto: se energiza la electrocerradura, el cliente abre, retira y cierra la puerta,
    y la cerradura vuelve a bloquearse."""
    estado = EstadoCerradura.BLOQUEADA
    eventos: list[Evento] = []

    def cambiar(t, nuevo):
        nonlocal estado
        eventos.append(Evento(t, "ESTADO", f"{estado.value} -> {nuevo.value}"))
        estado = nuevo

    eventos.append(Evento(0.0, "SENAL", f"ELECTROCERRADURA_{casillero}=ON"))
    cambiar(0.0, EstadoCerradura.ABIERTA)
    eventos.append(Evento(0.5, "SENSOR", f"PUERTA_{casillero}=ABIERTA"))
    eventos.append(Evento(segundos_abierta, "SENSOR", f"PUERTA_{casillero}=CERRADA"))
    eventos.append(Evento(segundos_abierta, "SENAL", f"ELECTROCERRADURA_{casillero}=OFF"))
    cambiar(segundos_abierta, EstadoCerradura.BLOQUEADA)
    return eventos
