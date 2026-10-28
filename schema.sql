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
    ('timeout_atasco_s',      '10',  'Segundos sin avance del sensor antes de declarar atasco'),
    ('horas_sesion',          '12',  'Horas que dura una sesión iniciada antes de pedir la contraseña otra vez'),
    ('peso_max_bascula_g',    '50000', 'Peso máximo creíble de la celda de carga (g); más que esto se toma como falla de la báscula');

-- ---------------------------------------------------------------- cuentas (Issue 1)
-- CLIENTE: se registra solo desde la pantalla del cliente.
-- ADMIN:   lo crea quien instala el sistema con `python crear_admin.py` (no hay registro público).
-- La contraseña nunca se guarda: solo su hash PBKDF2 con sal aleatoria.
CREATE TABLE usuarios (
    id            INTEGER PRIMARY KEY,
    correo        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    usuario       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    salt          TEXT    NOT NULL,
    rol           TEXT    NOT NULL CHECK (rol IN ('CLIENTE', 'ADMIN')),
    activo        INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1)),
    creado_en     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Sesión iniciada. Se guarda el hash del token, no el token: quien lea la BD no puede
-- hacerse pasar por nadie.
CREATE TABLE sesiones (
    token_hash TEXT    PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios (id),
    creado_en  TEXT    NOT NULL,
    expira_en  TEXT    NOT NULL
);

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
-- Un pedido = un carrito de compras = UN casillero. Los productos del carrito
-- (uno o varios, distintos entre sí) se empacan juntos en la misma caja y se
-- verifican con un solo peso: la suma de cantidad x peso unitario de sus líneas
-- (tabla pedido_items).
--
-- PENDIENTE_PAGO el cliente armó el carrito; aún no paga. NO descuenta inventario (RF-05)
-- EN_COLA         pagado, sin casillero libre todavía
-- ASIGNADO        casillero reservado, esperando arrancar la banda
-- EN_TRANSPORTE   banda/actuador moviendo la caja
-- VERIFICANDO     en el casillero, validando peso y foto
-- LISTO_RETIRO    verificado, PIN vigente, esperando al cliente
-- ENTREGADO       el cliente digitó el PIN y retiró
-- RECHAZADO       el peso no coincidió (Error de Despacho)
-- ATASCADO        la banda se detuvo por atasco
-- CANCELADO       cancelado por el cliente
-- CADUCADO        pasaron 24 h sin retiro
CREATE TABLE pedidos (
    id               INTEGER PRIMARY KEY,
    usuario_id       INTEGER REFERENCES usuarios (id),  -- cuenta del cliente que pidió
    cliente_nombre   TEXT    NOT NULL,
    cliente_contacto TEXT    NOT NULL,
    peso_teorico_g   REAL    NOT NULL CHECK (peso_teorico_g > 0),
    pagado_en        TEXT,                              -- NULL = aún no se ha descontado stock
    estado           TEXT    NOT NULL DEFAULT 'PENDIENTE_PAGO'
                     CHECK (estado IN ('PENDIENTE_PAGO', 'EN_COLA', 'ASIGNADO', 'EN_TRANSPORTE', 'VERIFICANDO',
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
CREATE INDEX ix_pedidos_cola ON pedidos (estado, id);
CREATE INDEX ix_pedidos_usuario ON pedidos (usuario_id, id);

-- Líneas del carrito. peso_unitario_g es una copia (snapshot) del peso del producto
-- al momento de pedir: si luego cambia el catálogo, el peso teórico no se altera.
-- cantidad_pedida: lo que el cliente pidió. cantidad: lo que se confirmó al pagar (RF-05):
-- si al pagar quedaban menos unidades, el pedido sigue con lo disponible (puede quedar en 0).
CREATE TABLE pedido_items (
    pedido_id       INTEGER NOT NULL REFERENCES pedidos (id),
    producto_id     INTEGER NOT NULL REFERENCES productos (id),
    cantidad        INTEGER NOT NULL CHECK (cantidad >= 0),
    peso_unitario_g REAL    NOT NULL CHECK (peso_unitario_g > 0),
    cantidad_pedida INTEGER NOT NULL CHECK (cantidad_pedida > 0),
    PRIMARY KEY (pedido_id, producto_id)
);

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
                 CHECK (tipo IN ('ATASCO', 'ERROR_DESPACHO', 'STOCK_BAJO', 'PIN_BLOQUEADO', 'BASCULA')),
    pedido_id    INTEGER REFERENCES pedidos (id),
    casillero_id INTEGER REFERENCES casilleros (id),
    producto_id  INTEGER REFERENCES productos (id),
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

-- ---------------------------------------------------------------- notificaciones
-- Bandeja de salida SIMULADA hacia el cliente (en producción sería SMS/correo/push
-- y no se guardaría el PIN en claro: aquí se guarda solo para poder mostrarlo).
CREATE TABLE notificaciones (
    id        INTEGER PRIMARY KEY,
    pedido_id INTEGER NOT NULL REFERENCES pedidos (id),
    mensaje   TEXT    NOT NULL,
    creado_en TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
