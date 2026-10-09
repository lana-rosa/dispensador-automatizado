# Dispensador automatizado — MVP (Python)

Máquina dispensadora simulada con casilleros inteligentes, PIN, validación de peso y foto.
Todo el hardware es simulado. Carpeta independiente del sitio web.

## Cómo verlo funcionar

Requiere Python 3.10+.

```bash
cd dispensador
pip install -r requirements.txt

# A) Recorrido narrado en consola (P1, P2, P3, P5, P6) — lo más rápido para ver el flujo
python3 demo_consola.py

# B) Programa con pantallas
python3 db.py                       # (opcional) recrea la BD con datos de demostración
python3 -m uvicorn api:app --reload
# abre http://127.0.0.1:8000
```

| Dirección | Pantalla |
|---|---|
| http://127.0.0.1:8000/cliente | Cliente: catálogo, carrito, mis pedidos (con PIN) y teclado del casillero |
| http://127.0.0.1:8000/admin | Administrador: ocupación, casilleros, pedidos, inventario, alertas, simulador de la máquina y reloj simulado |
| http://127.0.0.1:8000/docs | API técnica (para desarrolladores) |

Flujo típico: en **Cliente** arma el carrito y pide; en **Administrador**, en «Simulador de la máquina»,
lleva la caja al casillero y pésala (cambia el peso para simular un error); vuelve a **Cliente**,
pulsa «Ir a retirar» y digita el PIN en el teclado. Para probar la caducidad, en el administrador
pulsa «+24 h» en el reloj simulado.

En `/docs` cada endpoint tiene el botón **Try it out**. Flujo sugerido:

1. `GET /productos` — ver qué hay.
2. `POST /pedidos` — crear un carrito, ej. `{"cliente_nombre":"Ana","cliente_contacto":"ana@x.co","items":[{"sku":"ARD-UNO","cantidad":2}]}`.
3. `POST /maquina/pedidos/{id}/llegada` — (provisional) la banda deja la caja en el casillero.
4. `POST /maquina/pedidos/{id}/verificacion` — `{"peso_medido_g": 50, "foto_ruta": "fotos/p1.jpg"}`.
   Si el peso coincide se genera el PIN; si no, queda RECHAZADO y se crea la alerta.
5. `GET /pedidos/{id}/notificaciones` — aquí «llega» el PIN al cliente.
6. `POST /casilleros/{codigo}/abrir` — `{"pin": "123456"}`.
7. `GET /admin/casilleros`, `/admin/inventario`, `/admin/alertas` — vista del administrador.
8. `GET /pedidos/{id}/trazabilidad` — historial completo del pedido.

Para probar P5 en vivo sin esperar 24 h, usa la demo (A) o las pruebas: el reloj es inyectable.

## Pruebas

```bash
python3 -m unittest discover -s pruebas -v
```

## Estructura

| Archivo | Qué hace |
|---|---|
| `schema.sql` | Tablas, restricciones e índices (SQLite) |
| `db.py` | Conexión y datos de demostración |
| `servicio.py` | Todas las reglas de negocio |
| `api.py` | Endpoints REST (FastAPI) y rutas de las pantallas |
| `pantallas/` | Pantallas de cliente y administrador (HTML/CSS/JS, sin internet) |
| `demo_consola.py` | Recorrido narrado de los casos de prueba |
| `pruebas/` | Pruebas automáticas |

## Estado

- [x] Paso 1 — Base de datos
- [x] Paso 2 — Backend (pedido, PIN, peso, caducidad, cola, admin)
- [x] Pantallas de cliente y administrador
- [ ] Paso 3 — Máquina de estados: banda, desvío, electrocerraduras, sensor y atasco (P4)
