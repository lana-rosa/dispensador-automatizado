-- Esquema del MVP: máquina dispensadora con casilleros inteligentes.
-- Fechas: texto ISO-8601 en UTC (ej. 2026-10-08T14:30:00Z). Pesos en gramos.

-- ---------------------------------------------------------------- configuración
-- Parámetros que la lógica lee en vez de tenerlos "quemados" en el código.
CREATE TABLE configuracion (
    clave       TEXT PRIMARY KEY,
    valor       TEXT NOT NULL,
    descripcion TEXT
);

INSERT INTO configuracion (clave, valor, descripcion) VALUES
    ('horas_reserva',         '24',  'Horas que el cliente tiene para retirar antes de que caduque el PIN'),
    ('longitud_pin',          '6',   'Dígitos del PIN'),
    ('max_intentos_pin',      '3',   'Intentos fallidos antes de bloquear el PIN'),
    ('tolerancia_peso_pct',   '2.0', 'Tolerancia de peso en % del peso teórico'),
    ('tolerancia_peso_min_g', '2.0', 'Tolerancia mínima en gramos (para piezas muy livianas)'),
    ('timeout_atasco_s',      '10',  'Segundos sin avance del sensor antes de declarar atasco');

-- ---------------------------------------------------------------- inventario
-- stock_disponible: unidades que se pueden ofrecer a nuevos pedidos.
-- stock_reservado:  unidades comprometidas con pedidos aún no entregados.
-- Total físico en bodega = disponible + reservado.
CREATE TABLE productos (
    id               INTEGER PRIMARY KEY,
    sku              TEXT    NOT NULL UNIQUE,
    nombre           TEXT    NOT NULL,
    categoria        TEXT    NOT NULL
                     CHECK (categoria IN ('COMPONENTE', 'HERRAMIENTA', 'INSUMO_LAB')),
    peso_unitario_g  REAL    NOT NULL CHECK (peso_unitario_g > 0),
    stock_disponible INTEGER NOT NULL DEFAULT 0 CHECK (stock_disponible >= 0),
    stock_reservado  INTEGER NOT NULL DEFAULT 0 CHECK (stock_reservado >= 0),
    stock_minimo     INTEGER NOT NULL DEFAULT 0 CHECK (stock_minimo >= 0),
    activo           INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1))
);

-- ---------------------------------------------------------------- casilleros
-- LIBRE         : vacío y asignable.
-- RESERVADO     : asignado a un pedido; el producto va en camino.
-- OCUPADO       : producto dentro, verificado, esperando que el cliente lo retire.
-- MANTENIMIENTO : fuera de servicio (no se asigna).
CREATE TABLE casilleros (
    id             INTEGER PRIMARY KEY,
    codigo         TEXT NOT NULL UNIQUE,
    estado         TEXT NOT NULL DEFAULT 'LIBRE'
                   CHECK (estado IN ('LIBRE', 'RESERVADO', 'OCUPADO', 'MANTENIMIENTO')),
    actualizado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ---------------------------------------------------------------- pedidos
-- Regla del proyecto: no se mezclan productos distintos en una misma caja.
-- Por eso un pedido = UN producto x cantidad = UN casillero. Si el carrito de la
-- app tiene varios productos, se crea un pedido por producto y se agrupan con
-- carrito_ref.
--
-- EN_COLA         aceptado, sin casillero libre todavía
-- ASIGNADO        casillero reservado, esperando arrancar la banda
-- EN_TRANSPORTE   banda/actuador moviendo el producto
-- VERIFICANDO     en el casillero, validando peso y foto
-- LISTO_RETIRO    verificado, PIN vigente, esperando al cliente
-- ENTREGADO       el cliente digitó el PIN y retiró
-- RECHAZADO       el peso no coincidió (Error de Despacho)
-- ATASCADO        la banda se detuvo por atasco
-- CANCELADO       cancelado por el cliente
-- CADUCADO        pasaron 24 h sin retiro
CREATE TABLE pedidos (
    id               INTEGER PRIMARY KEY,
    cliente_nombre   TEXT    NOT NULL,
    cliente_contacto TEXT    NOT NULL,
    carrito_ref      TEXT,
    producto_id      INTEGER NOT NULL REFERENCES productos (id),
    cantidad         INTEGER NOT NULL CHECK (cantidad > 0),
    peso_teorico_g   REAL    NOT NULL CHECK (peso_teorico_g > 0),
    estado           TEXT    NOT NULL DEFAULT 'EN_COLA'
                     CHECK (estado IN ('EN_COLA', 'ASIGNADO', 'EN_TRANSPORTE', 'VERIFICANDO',
                                       'LISTO_RETIRO', 'ENTREGADO', 'RECHAZADO', 'ATASCADO',
                                       'CANCELADO', 'CADUCADO')),
    casillero_id     INTEGER REFERENCES casilleros (id),
    creado_en        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    actualizado_en   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- Coherencia: en cola => sin casillero; en proceso => con casillero.
    CHECK (estado <> 'EN_COLA' OR casillero_id IS NULL),
    CHECK (estado NOT IN ('ASIGNADO', 'EN_TRANSPORTE', 'VERIFICANDO', 'LISTO_RETIRO', 'ATASCADO')
           OR casillero_id IS NOT NULL)
);

-- Un casillero solo puede tener UN pedido en proceso a la vez.
CREATE UNIQUE INDEX ux_pedido_activo_por_casillero
    ON pedidos (casillero_id)
    WHERE casillero_id IS NOT NULL
      AND estado IN ('ASIGNADO', 'EN_TRANSPORTE', 'VERIFICANDO', 'LISTO_RETIRO', 'ATASCADO');

-- Para sacar rápido el siguiente pedido de la cola (FIFO).
CREATE INDEX ix_pedidos_cola ON pedidos (estado, creado_en);

-- ---------------------------------------------------------------- PIN
-- Se guarda el hash, nunca el PIN en claro: el PIN solo viaja al cliente.
CREATE TABLE pins (
    id                INTEGER PRIMARY KEY,
    pedido_id         INTEGER NOT NULL REFERENCES pedidos (id),
    casillero_id      INTEGER NOT NULL REFERENCES casilleros (id),
    pin_hash          TEXT    NOT NULL,
    salt              TEXT    NOT NULL,
    estado            TEXT    NOT NULL DEFAULT 'VIGENTE'
                      CHECK (estado IN ('VIGENTE', 'USADO', 'CADUCADO', 'BLOQUEADO')),
    intentos_fallidos INTEGER NOT NULL DEFAULT 0 CHECK (intentos_fallidos >= 0),
    creado_en         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expira_en         TEXT    NOT NULL
);

-- Un casillero y un pedido tienen como máximo un PIN vigente.
CREATE UNIQUE INDEX ux_pin_vigente_casillero ON pins (casillero_id) WHERE estado = 'VIGENTE';
CREATE UNIQUE INDEX ux_pin_vigente_pedido    ON pins (pedido_id)    WHERE estado = 'VIGENTE';

-- ---------------------------------------------------------------- verificación
-- Un registro por intento de validación (peso medido + foto simulada).
CREATE TABLE verificaciones (
    id             INTEGER PRIMARY KEY,
    pedido_id      INTEGER NOT NULL REFERENCES pedidos (id),
    peso_teorico_g REAL    NOT NULL,
    peso_medido_g  REAL    NOT NULL CHECK (peso_medido_g >= 0),
    tolerancia_g   REAL    NOT NULL CHECK (tolerancia_g >= 0),
    aprobado       INTEGER NOT NULL CHECK (aprobado IN (0, 1)),
    foto_ruta      TEXT,
    creado_en      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ---------------------------------------------------------------- alertas
CREATE TABLE alertas (
    id           INTEGER PRIMARY KEY,
    tipo         TEXT    NOT NULL
                 CHECK (tipo IN ('ATASCO', 'ERROR_DESPACHO', 'STOCK_BAJO', 'PIN_BLOQUEADO')),
    pedido_id    INTEGER REFERENCES pedidos (id),
    casillero_id INTEGER REFERENCES casilleros (id),
    mensaje      TEXT    NOT NULL,
    atendida     INTEGER NOT NULL DEFAULT 0 CHECK (atendida IN (0, 1)),
    creado_en    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    atendida_en  TEXT
);

-- ---------------------------------------------------------------- bitácora
-- Trazabilidad reserva -> entrega: cada cambio de estado y cada señal simulada
-- (banda ON, actuador, cerradura abierta...) deja una fila.
CREATE TABLE bitacora (
    id           INTEGER PRIMARY KEY,
    pedido_id    INTEGER REFERENCES pedidos (id),
    casillero_id INTEGER REFERENCES casilleros (id),
    tipo         TEXT NOT NULL,
    detalle      TEXT,
    creado_en    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX ix_bitacora_pedido ON bitacora (pedido_id, id);
