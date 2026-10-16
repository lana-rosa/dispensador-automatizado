// Funciones compartidas por las pantallas de cliente y administrador.

// ---------------------------------------------------------------- sesión (Issue 1)
// Cada pantalla guarda su propia sesión (<body data-sesion="cliente|admin">), así un
// administrador y un cliente pueden tener sesión a la vez en el mismo navegador.
let sesionEnMemoria = null;  // respaldo si el navegador bloquea localStorage

function claveSesion() { return "dispensador.sesion." + (document.body.dataset.sesion || "general"); }

function leerSesion() {
  try { return JSON.parse(localStorage.getItem(claveSesion())) || sesionEnMemoria; }
  catch (_) { return sesionEnMemoria; }
}

function guardarSesion(s) {
  sesionEnMemoria = s;
  try { localStorage.setItem(claveSesion(), JSON.stringify(s)); } catch (_) {}
}

function borrarSesion() {
  sesionEnMemoria = null;
  try { localStorage.removeItem(claveSesion()); } catch (_) {}
}

async function api(ruta, opciones = {}) {
  const headers = { "Content-Type": "application/json" };
  const sesion = leerSesion();
  if (sesion && sesion.token) headers.Authorization = `Bearer ${sesion.token}`;
  const resp = await fetch(ruta, {
    ...opciones,
    headers,
    body: opciones.body ? JSON.stringify(opciones.body) : undefined,
  });
  let datos = null;
  try { datos = await resp.json(); } catch (_) { /* respuesta sin cuerpo */ }
  // Sesión vencida o cerrada: la pantalla decide qué mostrar (define alPerderSesion).
  if (resp.status === 401 && sesion && typeof alPerderSesion === "function") alPerderSesion();
  return { ok: resp.ok, status: resp.status, datos };
}

// Convierte cualquier error de la API en un texto entendible.
function textoError(r) {
  const d = r.datos;
  if (!d) return `Error ${r.status}`;
  if (d.detalle) return d.detalle;
  if (Array.isArray(d.detail)) {
    return d.detail.map((e) => `${(e.loc || []).slice(-1)[0]}: ${e.msg}`).join(" · ");
  }
  return d.detail || `Error ${r.status}`;
}

// Escapa texto para insertarlo en HTML (nombres de clientes, mensajes...).
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function fecha(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("es-CO", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

const ESTADOS = {
  EN_COLA: ["En cola", "ambar", "Esperando un casillero libre"],
  ASIGNADO: ["Casillero reservado", "azul", "Preparando tu pedido"],
  EN_TRANSPORTE: ["En camino", "azul", "La banda lo lleva al casillero"],
  VERIFICANDO: ["Verificando", "lila", "Revisando peso y foto"],
  LISTO_RETIRO: ["Listo para retirar", "verde", "Usa tu PIN en el casillero"],
  ENTREGADO: ["Entregado", "", "Pedido retirado"],
  RECHAZADO: ["Error de despacho", "rojo", "El peso no coincidió; un asesor te contactará"],
  ATASCADO: ["Detenido", "rojo", "Atasco en la banda"],
  CANCELADO: ["Cancelado", "", "Cancelado por el cliente"],
  CADUCADO: ["Caducado", "", "No se retiró dentro de las 24 h"],
};

function chipEstado(estado) {
  const [texto, color] = ESTADOS[estado] || [estado, ""];
  return `<span class="chip ${color}">${esc(texto)}</span>`;
}

function resumenItems(items) {
  return items.map((i) => `${i.cantidad} × ${esc(i.nombre)}`).join(", ");
}
