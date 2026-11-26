"""
alertas_sobrecarga.py
---------------------
ALERTAS DE SOBRECARGA QUE ACTUAN.

Vigila la carga de trabajo mental de cada ROL en una ventana movil (los
ultimos VENTANA_MIN minutos). Cuando un rol supera el umbral de carga ALTA,
el sistema no solo avisa: los agentes ACTUAN.

  1. Prioriza en cocina los pedidos atrasados del turno: el agente Mesero
     envia por XMPP la senal 'apurar_pedido' (motivo: sobrecarga) al agente
     Cocinero, y la cocina los ve marcados como PRIORIDAD.
  2. Sugiere refuerzo para el puesto y una pausa breve para el trabajador
     mas cargado.
  3. Avisa al administrador y al equipo del puesto (notificacion en pantalla)
     y deja constancia en el monitor y en la bitacora (alertas_sobrecarga).

Se evalua cada CHEQUEO_SEG y tambien justo despues de cada mensaje del chat.
Tras una alerta, el mismo rol no vuelve a alertar durante ENFRIAMIENTO_MIN.
"""
import asyncio
import json

import aiosqlite

import agent_runtime
import agentes
import db_utils as db
import monitor

VENTANA_MIN = 30            # ventana movil de lecturas
MIN_LECTURAS = 6            # minimo de lecturas para evaluar un rol
UMBRAL_ALTA = 35.0          # % de carga alta que dispara la alerta
UMBRAL_CRITICO = 50.0       # % de carga alta considerado critico
ENFRIAMIENTO_MIN = 15       # no repetir alerta del mismo rol antes de esto
CHEQUEO_SEG = 60            # frecuencia de la vigilancia periodica
ATRASO_PENDIENTE_SEG = 120  # pendiente hace mas de 2 min (meta SLA de toma: 60 s)
ATRASO_PREPARACION_SEG = 300  # en preparacion hace mas de 5 min (meta SLA: 300 s)
MAX_PRIORIZAR = 3           # pedidos que se priorizan por alerta
PAUSA_MIN = 10              # pausa sugerida al trabajador mas cargado

ROLES = ("mesero", "cocinero", "cajero", "almacenero")
ROL_NOMBRE = {"mesero": "Mesero", "cocinero": "Cocina", "cajero": "Caja", "almacenero": "Almacen"}
REFUERZO = {
    "mesero": "reforzar la atencion en sala",
    "cocinero": "sumar una persona en cocina",
    "cajero": "abrir una segunda caja o apoyar el cobro",
    "almacenero": "posponer las tareas no criticas del almacen",
}

_lock = asyncio.Lock()
_tareas = set()


def config() -> dict:
    return {
        "ventana_min": VENTANA_MIN, "min_lecturas": MIN_LECTURAS,
        "umbral_alta": UMBRAL_ALTA, "umbral_critico": UMBRAL_CRITICO,
        "enfriamiento_min": ENFRIAMIENTO_MIN, "chequeo_seg": CHEQUEO_SEG,
        "pausa_min": PAUSA_MIN,
    }


async def estado_ventana() -> list[dict]:
    """% de carga alta por rol en la ventana movil (lo que vigila el sistema)."""
    async with aiosqlite.connect(db.DB_PATH) as con:
        con.row_factory = aiosqlite.Row
        cur = await con.execute(
            "SELECT u.rol AS rol, COUNT(*) AS lecturas, SUM(ie.nivel = 'alto') AS alta "
            "FROM interacciones_estres ie JOIN usuarios u ON u.id = ie.usuario_id "
            "WHERE u.rol != 'admin' AND ie.fecha_hora >= datetime('now', 'localtime', ?) AND ie.fecha_hora <= datetime('now', 'localtime') "
            "GROUP BY u.rol",
            (f"-{VENTANA_MIN} minutes",),
        )
        filas = {r["rol"]: dict(r) for r in await cur.fetchall()}
    salida = []
    for rol in ROLES:
        f = filas.get(rol, {})
        n, alta = f.get("lecturas") or 0, f.get("alta") or 0
        pct = round(100.0 * alta / n, 1) if n else 0.0
        if n >= MIN_LECTURAS and pct >= UMBRAL_CRITICO:
            estado = "critica"
        elif n >= MIN_LECTURAS and pct >= UMBRAL_ALTA:
            estado = "alta"
        else:
            estado = "normal" if n else "sin_datos"
        salida.append({"rol": rol, "rol_nombre": ROL_NOMBRE[rol], "lecturas": n,
                       "alta": alta, "pct_alta": pct, "estado": estado})
    return salida


async def _en_enfriamiento(rol: str) -> bool:
    async with aiosqlite.connect(db.DB_PATH) as con:
        cur = await con.execute(
            "SELECT 1 FROM alertas_sobrecarga WHERE rol = ? "
            "AND fecha_hora >= datetime('now', 'localtime', ?) LIMIT 1",
            (rol, f"-{ENFRIAMIENTO_MIN} minutes"),
        )
        return await cur.fetchone() is not None


async def _trabajador_mas_cargado(rol: str) -> dict | None:
    async with aiosqlite.connect(db.DB_PATH) as con:
        con.row_factory = aiosqlite.Row
        cur = await con.execute(
            "SELECT u.id AS id, u.nombre AS nombre, COUNT(*) AS lecturas, SUM(ie.nivel = 'alto') AS alta "
            "FROM interacciones_estres ie JOIN usuarios u ON u.id = ie.usuario_id "
            "WHERE u.rol = ? AND ie.fecha_hora >= datetime('now', 'localtime', ?) AND ie.fecha_hora <= datetime('now', 'localtime') "
            "GROUP BY u.id ORDER BY alta DESC, lecturas DESC LIMIT 1",
            (rol, f"-{VENTANA_MIN} minutes"),
        )
        fila = await cur.fetchone()
        return dict(fila) if fila else None


async def _pedidos_atrasados() -> list[dict]:
    """Pedidos del turno actual (ultimas 12 h) que ya superaron la meta de SLA."""
    async with aiosqlite.connect(db.DB_PATH) as con:
        con.row_factory = aiosqlite.Row
        cur = await con.execute(
            "SELECT id, estado, fecha_hora FROM pedidos "
            "WHERE fecha_hora >= datetime('now', 'localtime', '-12 hours') AND ("
            "  (estado = 'pendiente' AND fecha_hora <= datetime('now', 'localtime', ?)) OR "
            "  (estado = 'en_preparacion' AND COALESCE(fecha_inicio_preparacion, fecha_hora) "
            "      <= datetime('now', 'localtime', ?))"
            ") ORDER BY fecha_hora",
            (f"-{ATRASO_PENDIENTE_SEG} seconds", f"-{ATRASO_PREPARACION_SEG} seconds"),
        )
        filas = [dict(r) for r in await cur.fetchall()]
    ya_marcados = agentes.PEDIDOS_URGENTES
    return [f for f in filas if f["id"] not in ya_marcados][:MAX_PRIORIZAR]


async def _priorizar_en_cocina(pedido_id: int, detalle: str) -> str:
    """El agente Mesero pide a Cocina priorizar el pedido (XMPP). Si el agente
    no esta disponible, se marca directo para que la cocina igual lo vea."""
    if agent_runtime.agente_disponible("mesero"):
        try:
            async def _enviar():
                ag = agent_runtime.agentes["mesero"]
                behav = ag.MarcarUrgenteBehav(pedido_id, detalle, motivo="sobrecarga")
                ag.add_behaviour(behav)
                await behav.join()
            await agent_runtime.ejecutar(_enviar())
            return "agentes"
        except Exception as e:
            print(f"[alertas] Aviso: no se pudo enviar la prioridad por XMPP ({e}); se marca directo.")
    agentes.marcar_urgente(pedido_id, "sobrecarga")
    return "directo"


def _sugerencia(rol: str, trabajador: dict | None, nivel: str) -> str:
    texto = f"Se sugiere {REFUERZO[rol]}"
    if trabajador and (trabajador.get("alta") or 0) > 0:
        texto += (f" y una pausa breve de {PAUSA_MIN} min para {trabajador['nombre']} "
                  f"({trabajador['alta']} lecturas de carga alta en {VENTANA_MIN} min)")
    texto += "."
    return ("URGENTE: " + texto) if nivel == "critica" else texto


async def _actuar(v: dict) -> dict:
    rol, pct, n = v["rol"], v["pct_alta"], v["lecturas"]
    nivel = "critica" if pct >= UMBRAL_CRITICO else "alta"
    nombre_rol = ROL_NOMBRE[rol]
    trabajador = await _trabajador_mas_cargado(rol)
    acciones, priorizados = [], []

    if rol in ("mesero", "cocinero"):
        via = None
        for p in await _pedidos_atrasados():
            via = await _priorizar_en_cocina(p["id"], f"Sobrecarga en {nombre_rol} ({pct}% de carga alta)")
            priorizados.append(p["id"])
        if priorizados:
            lista = ", ".join(f"#{i}" for i in priorizados)
            canal = "el agente Mesero aviso al agente Cocinero por XMPP" if via == "agentes" else "marcado directo en cocina"
            acciones.append(f"Se priorizaron en cocina los pedidos atrasados {lista} ({canal}).")
        else:
            acciones.append("No habia pedidos atrasados del turno que priorizar en cocina.")

    sugerencia = _sugerencia(rol, trabajador, nivel)
    acciones.append("Se notifico al administrador y al equipo del puesto.")

    async with aiosqlite.connect(db.DB_PATH) as con:
        cur = await con.execute(
            "INSERT INTO alertas_sobrecarga (rol, nivel, lecturas, pct_alta, trabajador_id, trabajador, "
            "pedidos_priorizados, acciones, sugerencia) VALUES (?,?,?,?,?,?,?,?,?)",
            (rol, nivel, n, pct,
             trabajador["id"] if trabajador else None, trabajador["nombre"] if trabajador else None,
             ",".join(str(i) for i in priorizados), json.dumps(acciones, ensure_ascii=False), sugerencia),
        )
        alerta_id = cur.lastrowid
        await con.commit()

    monitor.registrar_evento(
        "error" if nivel == "critica" else "warning",
        f"Sobrecarga {nivel} en {nombre_rol}: {pct}% de carga alta en los ultimos {VENTANA_MIN} min ({n} lecturas)",
        origen="alertas",
    )
    monitor.registrar_actividad_agente(
        rol, f"Detecto sobrecarga ({pct}% carga alta) y actuo",
        " ".join(acciones[:-1]) + " " + sugerencia,
    )
    print(f"[alertas] Sobrecarga {nivel} en {nombre_rol} ({pct}%, {n} lecturas). Priorizados: {priorizados}")
    return (await listar(limite=1, alerta_id=alerta_id))[0]


async def evaluar() -> list[dict]:
    """Revisa todos los roles y actua sobre los que esten sobrecargados."""
    async with _lock:
        nuevas = []
        for v in await estado_ventana():
            if v["estado"] not in ("alta", "critica"):
                continue
            if await _en_enfriamiento(v["rol"]):
                continue
            nuevas.append(await _actuar(v))
        return nuevas


async def _evaluar_seguro():
    try:
        await evaluar()
    except Exception as e:
        print(f"[alertas] Aviso al evaluar sobrecarga: {e}")


def evaluar_en_segundo_plano():
    """Dispara una evaluacion sin bloquear (se usa tras cada mensaje del chat)."""
    tarea = asyncio.create_task(_evaluar_seguro())
    _tareas.add(tarea)
    tarea.add_done_callback(_tareas.discard)


async def bucle():
    """Vigilancia periodica (se inicia en el arranque de la app)."""
    await asyncio.sleep(5)
    while True:
        await _evaluar_seguro()
        await asyncio.sleep(CHEQUEO_SEG)


async def listar(limite: int = 30, desde_id: int | None = None, rol: str | None = None,
                 alerta_id: int | None = None) -> list[dict]:
    conds, params = [], []
    if alerta_id is not None:
        conds.append("id = ?"); params.append(alerta_id)
    if desde_id is not None:
        conds.append("id > ?"); params.append(desde_id)
    if rol:
        conds.append("rol = ?"); params.append(rol)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    async with aiosqlite.connect(db.DB_PATH) as con:
        con.row_factory = aiosqlite.Row
        cur = await con.execute(
            f"SELECT * FROM alertas_sobrecarga {where} ORDER BY id DESC LIMIT ?", (*params, limite))
        filas = [dict(r) for r in await cur.fetchall()]
    for f in filas:
        f["rol_nombre"] = ROL_NOMBRE.get(f["rol"], f["rol"])
        f["pedidos_priorizados"] = [int(x) for x in (f.get("pedidos_priorizados") or "").split(",") if x]
        try:
            f["acciones"] = json.loads(f.get("acciones") or "[]")
        except ValueError:
            f["acciones"] = []
    return filas


async def ultimo_id() -> int:
    async with aiosqlite.connect(db.DB_PATH) as con:
        cur = await con.execute("SELECT COALESCE(MAX(id), 0) FROM alertas_sobrecarga")
        return (await cur.fetchone())[0]


async def marcar_atendida(alerta_id: int) -> bool:
    async with aiosqlite.connect(db.DB_PATH) as con:
        cur = await con.execute("UPDATE alertas_sobrecarga SET atendida = 1 WHERE id = ?", (alerta_id,))
        await con.commit()
        return cur.rowcount > 0
