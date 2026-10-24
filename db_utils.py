"""
db_utils.py
------------
Funciones ASYNC de acceso a restaurante.db, pensadas para ser llamadas
desde dentro de behaviours de SPADE (que corren en un loop asyncio).
Usa aiosqlite en vez de sqlite3 para no bloquear el event loop.
"""
import os
import json
import aiosqlite

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restaurante.db")


async def obtener_platos_disponibles() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT nombre FROM platos WHERE disponible = 1 ORDER BY nombre")
        return [row[0] for row in await cur.fetchall()]


async def obtener_carta() -> list[dict]:
    """Carta completa (solo platos disponibles), ordenada por categoria."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT c.nombre AS categoria, p.nombre, p.precio FROM platos p "
            "JOIN categorias c ON c.id = p.categoria_id "
            "WHERE p.disponible = 1 ORDER BY c.orden, p.nombre"
        )
        return [dict(r) for r in await cur.fetchall()]


async def obtener_menu_para_rag() -> list[dict]:
    """
    Devuelve, por cada plato DISPONIBLE, su nombre, descripcion, categoria,
    precio y la lista de ingredientes REALES (tomados de receta_insumo).
    Es la fuente con la que menu_rag.py construye los documentos que se
    indexan en ChromaDB para responder consultas de ingredientes/alergenos.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT p.id, p.nombre, p.descripcion, p.precio, c.nombre AS categoria, "
            "  (SELECT GROUP_CONCAT(i.nombre, ', ') "
            "     FROM receta_insumo ri JOIN insumos i ON i.id = ri.insumo_id "
            "     WHERE ri.plato_id = p.id) AS ingredientes "
            "FROM platos p JOIN categorias c ON c.id = p.categoria_id "
            "WHERE p.disponible = 1 ORDER BY c.orden, p.nombre"
        )
        return [dict(r) for r in await cur.fetchall()]


async def obtener_plato_por_nombre(nombre: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, nombre, precio FROM platos WHERE nombre = ? AND disponible = 1", (nombre,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def crear_pedido(usuario_id: int | None, items: list[dict]) -> int:
    """
    items: lista de dicts como {"plato": "Superoso", "cantidad": 3}
    Devuelve el id del pedido creado. Lanza ValueError si algun plato no existe.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        cur = await db.execute("INSERT INTO pedidos (usuario_id) VALUES (?)", (usuario_id,))
        pedido_id = cur.lastrowid

        for item in items:
            plato = await obtener_plato_por_nombre(item["plato"])
            if not plato:
                raise ValueError(f"Plato no encontrado o no disponible: {item['plato']}")
            cantidad = item["cantidad"]
            subtotal = round(plato["precio"] * cantidad, 2)
            await db.execute(
                "INSERT INTO detalle_pedido (pedido_id, plato_id, cantidad, precio_unitario, subtotal) "
                "VALUES (?, ?, ?, ?, ?)",
                (pedido_id, plato["id"], cantidad, plato["precio"], subtotal),
            )
        await db.commit()
        return pedido_id


async def agregar_items_pedido(pedido_id: int, items: list[dict]) -> None:
    """
    Agrega nuevos items a un pedido YA EXISTENTE (ej: el cliente pide algo
    mas antes de que cocina termine de preparar todo). Lanza ValueError si
    el pedido no existe, ya no admite cambios, o un plato no existe.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,))
        row = await cur.fetchone()
        if not row:
            raise ValueError(f"Pedido #{pedido_id} no existe")
        if row["estado"] not in ("pendiente", "en_preparacion"):
            raise ValueError(
                f"Pedido #{pedido_id} ya esta en estado '{row['estado']}', no se pueden agregar items"
            )

        for item in items:
            plato = await obtener_plato_por_nombre(item["plato"])
            if not plato:
                raise ValueError(f"Plato no encontrado o no disponible: {item['plato']}")
            cantidad = item["cantidad"]
            subtotal = round(plato["precio"] * cantidad, 2)
            await db.execute(
                "INSERT INTO detalle_pedido (pedido_id, plato_id, cantidad, precio_unitario, subtotal) "
                "VALUES (?, ?, ?, ?, ?)",
                (pedido_id, plato["id"], cantidad, plato["precio"], subtotal),
            )
        await db.commit()


async def pedido_esta_abierto(pedido_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,))
        row = await cur.fetchone()
        return bool(row) and row[0] in ("pendiente", "en_preparacion")


async def obtener_pedidos_por_estado(estado: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM pedidos WHERE estado = ? ORDER BY fecha_hora ASC", (estado,))
        ids = [r["id"] for r in await cur.fetchall()]
    return [await obtener_pedido(pid) for pid in ids]


async def actualizar_stock_insumo(nombre_insumo: str, nuevo_stock: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM insumos WHERE nombre = ?", (nombre_insumo,))
        if not await cur.fetchone():
            raise ValueError(f"Insumo no encontrado: {nombre_insumo}")
        await db.execute("UPDATE insumos SET stock_actual = ? WHERE nombre = ?", (nuevo_stock, nombre_insumo))
        await db.commit()


async def obtener_todos_insumos() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nombre, unidad_medida, stock_actual, stock_minimo, fecha_vencimiento "
            "FROM insumos ORDER BY nombre"
        )
        return [dict(r) for r in await cur.fetchall()]


async def marcar_estado(pedido_id: int, nuevo_estado: str):
    """
    Cambia el estado del pedido y registra el timestamp de esa transicion
    (usado para calcular los SLA: tiempo de toma de pedido y tiempo de
    preparacion). Si el nuevo estado es 'listo', el trigger SQL de
    restaurante.db descuenta automaticamente el stock de insumos.
    """
    columna_timestamp = {
        "en_preparacion": "fecha_inicio_preparacion",
        "listo": "fecha_listo",
        "pagado": "fecha_pagado",
        "cancelado": "fecha_cancelado",
    }.get(nuevo_estado)

    async with aiosqlite.connect(DB_PATH) as db:
        if columna_timestamp:
            await db.execute(
                f"UPDATE pedidos SET estado = ?, {columna_timestamp} = datetime('now','localtime') WHERE id = ?",
                (nuevo_estado, pedido_id),
            )
        else:
            await db.execute("UPDATE pedidos SET estado = ? WHERE id = ?", (nuevo_estado, pedido_id))
        await db.commit()


async def cancelar_pedido(pedido_id: int) -> bool:
    """
    Cancela un pedido SOLO si todavia no fue pagado. Devuelve True si se
    pudo cancelar, False si ya estaba pagado (o no existe).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,))
        row = await cur.fetchone()
        if not row or row[0] in ("pagado", "cancelado"):
            return False
        await db.execute(
            "UPDATE pedidos SET estado = 'cancelado', fecha_cancelado = datetime('now','localtime') WHERE id = ?",
            (pedido_id,),
        )
        await db.commit()
        return True


async def listar_pedidos_por_estado(estados: list[str]) -> list[dict]:
    """Devuelve pedidos en cualquiera de los estados dados, con sus items, ordenados por fecha."""
    placeholders = ",".join("?" for _ in estados)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT id, estado, total, fecha_hora FROM pedidos WHERE estado IN ({placeholders}) ORDER BY fecha_hora",
            estados,
        )
        pedidos = [dict(r) for r in await cur.fetchall()]

        for pedido in pedidos:
            cur = await db.execute(
                "SELECT dp.cantidad, dp.subtotal, p.nombre FROM detalle_pedido dp "
                "JOIN platos p ON p.id = dp.plato_id WHERE dp.pedido_id = ?",
                (pedido["id"],),
            )
            pedido["items"] = [dict(r) for r in await cur.fetchall()]
        return pedidos


async def listar_usuarios() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, nombre, rol FROM usuarios WHERE activo = 1 ORDER BY rol, nombre")
        return [dict(r) for r in await cur.fetchall()]


async def sembrar_usuarios_demo():
    """Crea trabajadores de ejemplo si la tabla esta vacia (uno por rol).
    Para el despliegue real en DonOso, edita los nombres desde el panel
    de la BD o reemplazalos aqui con los trabajadores reales."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM usuarios")
        if (await cur.fetchone())[0] > 0:
            return
        demo = [
            ("Trabajador Mesero", "trabajador.mesero@127.0.0.1", "mesero"),
            ("Trabajador Cocina", "trabajador.cocina@127.0.0.1", "cocinero"),
            ("Trabajador Almacen", "trabajador.almacen@127.0.0.1", "almacenero"),
            ("Trabajador Caja", "trabajador.caja@127.0.0.1", "cajero"),
        ]
        await db.executemany("INSERT INTO usuarios (nombre, jid, rol) VALUES (?, ?, ?)", demo)
        await db.commit()
        print("[db_utils] Usuarios demo sembrados (edita los nombres para el despliegue real).")


# ---------------------------------------------------------------
# AUTENTICACION Y CREDENCIALES
# ---------------------------------------------------------------
# Credenciales por defecto por rol (username, password) para los usuarios
# demo. El admin se crea aparte. Cambialas desde el panel administrativo.
_CRED_DEMO = {
    "mesero": ("mesero", "mesero123"),
    "cocinero": ("cocina", "cocina123"),
    "almacenero": ("almacen", "almacen123"),
    "cajero": ("caja", "caja123"),
}


async def sembrar_credenciales_demo():
    """Asigna username/contrasena a los usuarios que aun no los tengan y crea
    un usuario admin si no existe. Idempotente. Las contrasenas se guardan
    cifradas con bcrypt (nunca en texto plano)."""
    import auth
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, rol, username FROM usuarios")
        filas = await cur.fetchall()
        for r in filas:
            if not r["username"]:
                user, pw = _CRED_DEMO.get(r["rol"], (f"user{r['id']}", "cambiar123"))
                await db.execute(
                    "UPDATE usuarios SET username = ?, password_hash = ? WHERE id = ?",
                    (user, auth.hashear(pw), r["id"]),
                )
                print(f"[db_utils] Credencial asignada: {user} (rol {r['rol']}).")

        cur = await db.execute("SELECT COUNT(*) FROM usuarios WHERE rol = 'admin'")
        if (await cur.fetchone())[0] == 0:
            await db.execute(
                "INSERT INTO usuarios (nombre, jid, rol, username, password_hash) VALUES (?, ?, ?, ?, ?)",
                ("Administrador", "admin@127.0.0.1", "admin", "admin", auth.hashear("admin123")),
            )
            print("[db_utils] Usuario admin creado (admin / admin123). CAMBIA la contrasena.")

        # Cuenta generica de usuario (rol mesero/mesero) para demo.
        cur = await db.execute("SELECT COUNT(*) FROM usuarios WHERE lower(username) = 'usuario'")
        if (await cur.fetchone())[0] == 0:
            await db.execute(
                "INSERT INTO usuarios (nombre, jid, rol, username, password_hash) VALUES (?, ?, ?, ?, ?)",
                ("Usuario", "usuario@127.0.0.1", "mesero", "usuario", auth.hashear("usuario123")),
            )
            print("[db_utils] Usuario generico creado (usuario / usuario123).")
        await db.commit()


async def autenticar(username: str, password: str) -> dict | None:
    """Devuelve el usuario si las credenciales son correctas y esta activo."""
    import auth
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, nombre, rol, username, password_hash FROM usuarios "
            "WHERE username = ? AND activo = 1",
            (username,),
        )
        row = await cur.fetchone()
    if not row or not auth.verificar(password, row["password_hash"]):
        return None
    return {"id": row["id"], "nombre": row["nombre"], "rol": row["rol"], "username": row["username"]}


async def exportar_pedidos_csv() -> str:
    """Todos los pedidos con sus tiempos y el trabajador que los tomo, en CSV."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT p.id, u.nombre AS trabajador, p.estado, p.total, p.fecha_hora, "
            "p.fecha_inicio_preparacion, p.fecha_listo, p.fecha_pagado, p.fecha_cancelado, "
            "ROUND((julianday(p.fecha_inicio_preparacion) - julianday(p.fecha_hora)) * 86400, 1) AS seg_toma, "
            "ROUND((julianday(p.fecha_listo) - julianday(p.fecha_inicio_preparacion)) * 86400, 1) AS seg_preparacion "
            "FROM pedidos p LEFT JOIN usuarios u ON u.id = p.usuario_id ORDER BY p.id"
        )
        filas = await cur.fetchall()

    columnas = ["id", "trabajador", "estado", "total", "fecha_hora", "fecha_inicio_preparacion",
                "fecha_listo", "fecha_pagado", "fecha_cancelado", "seg_toma", "seg_preparacion"]
    lineas = [";".join(columnas)]
    for f in filas:
        lineas.append(";".join("" if f[c] is None else str(f[c]) for c in columnas))
    return "\n".join(lineas)


async def exportar_estres_csv() -> str:
    """CSV de carga de trabajo mental por interaccion, con el trabajador."""
    _etiqueta = {"bajo": "Baja", "medio": "Media", "alto": "Alta"}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ie.id, ie.fecha_hora, COALESCE(u.nombre,'Sistema') AS trabajador, "
            "ie.nivel, ie.puntaje, ie.palabras_clave, ie.mensaje "
            "FROM interacciones_estres ie LEFT JOIN usuarios u ON u.id = ie.usuario_id ORDER BY ie.id"
        )
        filas = await cur.fetchall()
    lineas = ["id;fecha_hora;trabajador;carga_mental;puntaje;palabras_clave;mensaje"]
    for f in filas:
        mensaje = (f["mensaje"] or "").replace(";", ",").replace("\n", " ")
        trabajador = (f["trabajador"] or "").replace(";", ",")
        nivel = _etiqueta.get(f["nivel"], f["nivel"])
        lineas.append(f'{f["id"]};{f["fecha_hora"]};{trabajador};{nivel};{f["puntaje"]};{f["palabras_clave"] or ""};{mensaje}')
    return "\n".join(lineas)


async def ultimo_pedido_abierto() -> int | None:
    """Devuelve el id del pedido abierto mas reciente (o None). Se usa para
    recuperar el contexto del chat despues de reiniciar el servidor."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM pedidos WHERE estado IN ('pendiente','en_preparacion','listo') ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def metricas_sla(limite: int = 200, desde: str | None = None, hasta: str | None = None) -> dict:
    """
    Calcula los 2 SLA de la tesis, opcionalmente filtrados por rango de
    fechas (formato 'YYYY-MM-DD'):
    1) tiempo_toma: llegada del pedido -> inicio de preparacion.
    2) tiempo_preparacion: inicio de preparacion -> listo.
    Cada punto del detalle incluye la fecha_hora del pedido, para poder
    graficarlo como serie de tiempo.
    """
    condiciones = ["fecha_hora IS NOT NULL"]
    parametros: list = []
    if desde:
        condiciones.append("date(fecha_hora) >= date(?)")
        parametros.append(desde)
    if hasta:
        condiciones.append("date(fecha_hora) <= date(?)")
        parametros.append(hasta)
    parametros.append(limite)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT id, fecha_hora, fecha_inicio_preparacion, fecha_listo "
            f"FROM pedidos WHERE {' AND '.join(condiciones)} ORDER BY id ASC LIMIT ?",
            parametros,
        )
        filas = [dict(r) for r in await cur.fetchall()]

    detalle_toma = []
    detalle_preparacion = []
    for f in filas:
        if f["fecha_inicio_preparacion"]:
            segundos = await _diferencia_segundos(f["fecha_hora"], f["fecha_inicio_preparacion"])
            detalle_toma.append({"pedido_id": f["id"], "segundos": segundos, "fecha": f["fecha_hora"]})
        if f["fecha_inicio_preparacion"] and f["fecha_listo"]:
            segundos = await _diferencia_segundos(f["fecha_inicio_preparacion"], f["fecha_listo"])
            detalle_preparacion.append({"pedido_id": f["id"], "segundos": segundos, "fecha": f["fecha_hora"]})

    promedio_toma = round(sum(d["segundos"] for d in detalle_toma) / len(detalle_toma), 1) if detalle_toma else 0
    promedio_preparacion = (
        round(sum(d["segundos"] for d in detalle_preparacion) / len(detalle_preparacion), 1)
        if detalle_preparacion else 0
    )

    return {
        "tiempo_toma": {"promedio_segundos": promedio_toma, "detalle": detalle_toma},
        "tiempo_preparacion": {"promedio_segundos": promedio_preparacion, "detalle": detalle_preparacion},
    }


async def _diferencia_segundos(inicio_iso: str, fin_iso: str) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT (julianday(?) - julianday(?)) * 86400", (fin_iso, inicio_iso))
        row = await cur.fetchone()
        return round(row[0], 1) if row and row[0] is not None else 0.0


async def registrar_interaccion_estres(mensaje: str, puntaje: int, nivel: str,
                                       palabras: list[str], usuario_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO interacciones_estres (mensaje, puntaje, nivel, palabras_clave, usuario_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (mensaje, puntaje, nivel, ",".join(palabras), usuario_id),
        )
        await db.commit()


async def resumen_estres(limite: int = 50) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT mensaje, puntaje, nivel, palabras_clave, fecha_hora FROM interacciones_estres "
            "ORDER BY id DESC LIMIT ?",
            (limite,),
        )
        recientes = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT nivel, COUNT(*) as cantidad FROM interacciones_estres GROUP BY nivel"
        )
        conteo_por_nivel = {r["nivel"]: r["cantidad"] for r in await cur.fetchall()}

    return {"recientes": recientes, "conteo_por_nivel": conteo_por_nivel}


async def obtener_pedido(pedido_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        pedido = dict(await cur.fetchone())

        cur = await db.execute(
            "SELECT dp.cantidad, dp.subtotal, p.nombre FROM detalle_pedido dp "
            "JOIN platos p ON p.id = dp.plato_id WHERE dp.pedido_id = ?",
            (pedido_id,),
        )
        pedido["items"] = [dict(r) for r in await cur.fetchall()]
        return pedido


async def insumos_bajo_stock() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nombre, stock_actual, stock_minimo, unidad_medida FROM insumos "
            "WHERE stock_actual <= stock_minimo"
        )
        return [dict(r) for r in await cur.fetchall()]


async def insumos_por_vencer(dias: int = 3) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nombre, fecha_vencimiento FROM insumos "
            "WHERE fecha_vencimiento IS NOT NULL "
            "AND date(fecha_vencimiento) <= date('now', ?) "
            "ORDER BY fecha_vencimiento",
            (f"+{dias} days",),
        )
        return [dict(r) for r in await cur.fetchall()]


def to_json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def from_json(text: str):
    return json.loads(text)


# ---------------------------------------------------------------
# BUSQUEDA DE PEDIDOS POR CONTENIDO
# Para cuando el usuario dice "agrega tequenos al pedido de la
# donoso" o "como va el pedido de las alitas" — necesitamos
# encontrar pedidos que contengan un plato especifico.
# ---------------------------------------------------------------
async def buscar_pedidos_por_plato(nombre_plato: str, estados: list[str] | None = None) -> list[dict]:
    """Busca pedidos que contengan un plato especifico (coincidencia parcial).
    Si estados es None, busca en todos."""
    condicion_estado = ""
    params = [f"%{nombre_plato}%"]
    if estados:
        placeholders = ",".join("?" for _ in estados)
        condicion_estado = f" AND p.estado IN ({placeholders})"
        params.extend(estados)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT DISTINCT p.id, p.estado, p.total, p.fecha_hora "
            "FROM pedidos p JOIN detalle_pedido dp ON dp.pedido_id = p.id "
            "JOIN platos pl ON pl.id = dp.plato_id "
            f"WHERE pl.nombre LIKE ?{condicion_estado} "
            "ORDER BY p.id DESC LIMIT 10",
            params,
        )
        pedidos = [dict(r) for r in await cur.fetchall()]
        for pedido in pedidos:
            cur2 = await db.execute(
                "SELECT dp.cantidad, p.nombre FROM detalle_pedido dp "
                "JOIN platos p ON p.id = dp.plato_id WHERE dp.pedido_id = ?",
                (pedido["id"],),
            )
            pedido["items"] = [dict(r) for r in await cur2.fetchall()]
        return pedidos


async def pedidos_activos_del_turno(limite: int = 20) -> list[dict]:
    """Devuelve los pedidos abiertos (pendiente, en_preparacion, listo) del turno
    actual, utiles para que el mesero vea que tiene en curso."""
    return await listar_pedidos_por_estado(["pendiente", "en_preparacion", "listo"])


# ---------------------------------------------------------------
# ANULACION POST-COBRO (devoluciones / reembolsos)
# Caso real: ya se cobro pero hay un problema (plato mal hecho,
# cliente se queja, error de cobro). No se puede "des-pagar"
# un pedido con cancelar_pedido() porque ese solo funciona antes
# del pago. Se necesita un flujo distinto: el pedido pasa a
# estado "anulado" y se registra el motivo.
# ---------------------------------------------------------------
async def anular_pedido_cobrado(pedido_id: int, motivo: str) -> bool:
    """Anula un pedido YA PAGADO (equivale a un reembolso / nota de credito).
    Se marca como 'cancelado' y ADEMAS se guarda el motivo en
    motivo_anulacion: esa columna es la que distingue las anulaciones
    post-cobro (con motivo) de las cancelaciones pre-cobro (sin motivo).
    Solo funciona si el pedido esta en estado 'pagado'. Devuelve True si OK."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,))
        row = await cur.fetchone()
        if not row or row[0] != "pagado":
            return False
        await db.execute(
            "UPDATE pedidos SET estado = 'cancelado', "
            "fecha_cancelado = datetime('now','localtime'), motivo_anulacion = ? WHERE id = ?",
            (motivo or "sin motivo", pedido_id),
        )
        await db.commit()
        return True


async def listar_anulados_recientes(dias: int = 3) -> list[dict]:
    """Pedidos anulados post-cobro (los 'cancelado' que SI tienen motivo) en
    los ultimos N dias, para el apartado de anulaciones de caja. Incluye los
    items de cada uno."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, total, fecha_hora, fecha_cancelado, motivo_anulacion "
            "FROM pedidos WHERE estado = 'cancelado' AND motivo_anulacion IS NOT NULL "
            "AND datetime(fecha_cancelado) >= datetime('now','localtime', ?) "
            "ORDER BY fecha_cancelado DESC",
            (f"-{dias} days",),
        )
        anulados = [dict(r) for r in await cur.fetchall()]
        for p in anulados:
            cur2 = await db.execute(
                "SELECT dp.cantidad, dp.subtotal, pl.nombre FROM detalle_pedido dp "
                "JOIN platos pl ON pl.id = dp.plato_id WHERE dp.pedido_id = ?",
                (p["id"],),
            )
            p["items"] = [dict(r) for r in await cur2.fetchall()]
        return anulados


# ---------------------------------------------------------------
# FILAS PARA EXPORTACION (Excel / CSV) — devuelven list[dict] planos
# ---------------------------------------------------------------
async def filas_pedidos_export() -> list[dict]:
    """Todos los pedidos con sus tiempos, trabajador y motivo de anulacion."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT p.id, u.nombre AS trabajador, p.estado, p.total, p.fecha_hora, "
            "p.fecha_inicio_preparacion, p.fecha_listo, p.fecha_pagado, p.fecha_cancelado, "
            "p.motivo_anulacion, "
            "ROUND((julianday(p.fecha_inicio_preparacion) - julianday(p.fecha_hora)) * 86400, 1) AS seg_toma, "
            "ROUND((julianday(p.fecha_listo) - julianday(p.fecha_inicio_preparacion)) * 86400, 1) AS seg_preparacion "
            "FROM pedidos p LEFT JOIN usuarios u ON u.id = p.usuario_id ORDER BY p.id"
        )
        return [dict(r) for r in await cur.fetchall()]


async def filas_estres_export() -> list[dict]:
    """Todas las interacciones de CARGA DE TRABAJO MENTAL registradas, con el
    trabajador al que se atribuye cada lectura."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ie.id, ie.fecha_hora, ie.nivel, ie.puntaje, ie.palabras_clave, ie.mensaje, "
            "COALESCE(u.nombre, 'Sistema') AS trabajador "
            "FROM interacciones_estres ie "
            "LEFT JOIN usuarios u ON u.id = ie.usuario_id "
            "ORDER BY ie.id"
        )
        return [dict(r) for r in await cur.fetchall()]


# ---- Etiquetas legibles del nivel de carga mental ----
NIVEL_CARGA = {"bajo": "Baja", "medio": "Media", "alto": "Alta"}


ROLES_TRABAJADOR = ["mesero", "cocinero", "cajero", "almacenero"]


def _filtro_carga(rol: str | None = None, desde: str | None = None, hasta: str | None = None):
    """Devuelve (join, where, params) para acotar las consultas de carga mental
    por rol y/o rango de fechas. Las fechas son cadenas 'YYYY-MM-DD[ HH:MM:SS]'
    (se comparan directamente con ie.fecha_hora, que tiene ese formato)."""
    join = "LEFT JOIN usuarios u ON u.id = ie.usuario_id"
    conds, params = [], []
    if rol and rol not in ("todos", "", None):
        join = "JOIN usuarios u ON u.id = ie.usuario_id"
        conds.append("u.rol = ?")
        params.append(rol)
    if desde:
        conds.append("ie.fecha_hora >= ?")
        params.append(desde)
    if hasta:
        conds.append("ie.fecha_hora <= ?")
        params.append(hasta)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return join, where, params


# Compatibilidad: alias del filtro solo-rol.
def _filtro_rol(rol: str | None = None):
    return _filtro_carga(rol)


async def carga_mental_interacciones(rol: str | None = None, desde: str | None = None,
                                     hasta: str | None = None) -> list[dict]:
    """Filas crudas de carga mental (fecha, nivel, rol, trabajador). El panel las
    agrega en el navegador para filtrar por rol, fecha y orden sin recargar."""
    join, where, params = _filtro_carga(rol, desde, hasta)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ie.fecha_hora AS f, ie.nivel AS n, "
            "COALESCE(u.rol,'sin asignar') AS r, COALESCE(u.nombre,'Sistema') AS u "
            f"FROM interacciones_estres ie {join} {where} ORDER BY ie.fecha_hora",
            params,
        )
        return [dict(x) for x in await cur.fetchall()]


def _pct(parte, total):
    return round(100.0 * (parte or 0) / total, 1) if total else 0.0


async def carga_mental_diaria(rol: str | None = None, desde: str | None = None,
                              hasta: str | None = None) -> list[dict]:
    """Resumen por dia: conteo de carga baja/media/alta, total y % diario de cada
    nivel. Sirve para el Excel (%diario + total) y la linea de tendencia del admin.
    Opcionalmente acotado a un rol de trabajador y/o rango de fechas."""
    join, where, params = _filtro_carga(rol, desde, hasta)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT date(ie.fecha_hora) AS fecha, "
            "SUM(CASE WHEN ie.nivel='bajo' THEN 1 ELSE 0 END) AS baja, "
            "SUM(CASE WHEN ie.nivel='medio' THEN 1 ELSE 0 END) AS media, "
            "SUM(CASE WHEN ie.nivel='alto' THEN 1 ELSE 0 END) AS alta, "
            "COUNT(*) AS total "
            f"FROM interacciones_estres ie {join} {where} "
            "GROUP BY date(ie.fecha_hora) ORDER BY date(ie.fecha_hora)",
            params,
        )
        filas = []
        for r in await cur.fetchall():
            d = dict(r)
            total = d["total"] or 0
            d["pct_alta"] = _pct(d["alta"], total)
            d["pct_media"] = _pct(d["media"], total)
            d["pct_baja"] = _pct(d["baja"], total)
            filas.append(d)
        return filas


async def carga_mental_general(rol: str | None = None, desde: str | None = None,
                               hasta: str | None = None) -> dict:
    """Totales GENERALES (o de un rol/rango): conteo por nivel, total y % general
    de cada nivel. Es la cifra que debe cuadrar con el total de interacciones."""
    join, where, params = _filtro_carga(rol, desde, hasta)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT "
            "SUM(CASE WHEN ie.nivel='bajo' THEN 1 ELSE 0 END) AS baja, "
            "SUM(CASE WHEN ie.nivel='medio' THEN 1 ELSE 0 END) AS media, "
            "SUM(CASE WHEN ie.nivel='alto' THEN 1 ELSE 0 END) AS alta, "
            "COUNT(*) AS total "
            f"FROM interacciones_estres ie {join} {where}",
            params,
        )
        d = dict(await cur.fetchone())
    for k in ("baja", "media", "alta", "total"):
        d[k] = d[k] or 0
    total = d["total"]
    d["pct_baja"] = _pct(d["baja"], total)
    d["pct_media"] = _pct(d["media"], total)
    d["pct_alta"] = _pct(d["alta"], total)
    return d


async def carga_mental_por_rol(desde: str | None = None, hasta: str | None = None) -> list[dict]:
    """Carga mental agregada por ROL de trabajador (mesero, cocinero,
    cajero, almacenero). Total + % general de cada nivel por rol."""
    _, where, params = _filtro_carga(None, desde, hasta)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COALESCE(u.rol, 'sin asignar') AS rol, "
            "SUM(CASE WHEN ie.nivel='bajo' THEN 1 ELSE 0 END) AS baja, "
            "SUM(CASE WHEN ie.nivel='medio' THEN 1 ELSE 0 END) AS media, "
            "SUM(CASE WHEN ie.nivel='alto' THEN 1 ELSE 0 END) AS alta, "
            "COUNT(*) AS total "
            "FROM interacciones_estres ie "
            "LEFT JOIN usuarios u ON u.id = ie.usuario_id "
            f"{where} "
            "GROUP BY COALESCE(u.rol, 'sin asignar') ORDER BY total DESC",
            params,
        )
        filas = []
        for r in await cur.fetchall():
            d = dict(r)
            total = d["total"] or 0
            d["pct_baja"] = _pct(d["baja"], total)
            d["pct_media"] = _pct(d["media"], total)
            d["pct_alta"] = _pct(d["alta"], total)
            filas.append(d)
        return filas


async def carga_mental_diaria_por_rol() -> dict:
    """Serie diaria por ROL: para cada rol, lista de {fecha, baja, media, alta,
    total, pct_alta}. Permite filtrar la linea de tendencia por rol."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COALESCE(u.rol,'sin asignar') AS rol, date(ie.fecha_hora) AS fecha, "
            "SUM(CASE WHEN ie.nivel='bajo' THEN 1 ELSE 0 END) AS baja, "
            "SUM(CASE WHEN ie.nivel='medio' THEN 1 ELSE 0 END) AS media, "
            "SUM(CASE WHEN ie.nivel='alto' THEN 1 ELSE 0 END) AS alta, "
            "COUNT(*) AS total "
            "FROM interacciones_estres ie "
            "LEFT JOIN usuarios u ON u.id = ie.usuario_id "
            "GROUP BY COALESCE(u.rol,'sin asignar'), date(ie.fecha_hora) "
            "ORDER BY fecha"
        )
        series: dict = {}
        for r in await cur.fetchall():
            d = dict(r)
            total = d["total"] or 0
            d["pct_alta"] = _pct(d["alta"], total)
            series.setdefault(d["rol"], []).append(d)
        return series


async def carga_mental_por_usuario(rol: str | None = None, desde: str | None = None,
                                   hasta: str | None = None) -> list[dict]:
    """Carga mental agregada por trabajador: conteo por nivel, total y % de
    carga alta. Alimenta el grafico de pastel por usuario del panel admin.
    Opcionalmente acotado a un rol y/o rango de fechas."""
    join, where, params = _filtro_carga(rol, desde, hasta)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ie.usuario_id AS usuario_id, "
            "COALESCE(u.nombre, 'Sistema') AS trabajador, "
            "COALESCE(u.rol, '-') AS rol, "
            "SUM(CASE WHEN ie.nivel='bajo' THEN 1 ELSE 0 END) AS baja, "
            "SUM(CASE WHEN ie.nivel='medio' THEN 1 ELSE 0 END) AS media, "
            "SUM(CASE WHEN ie.nivel='alto' THEN 1 ELSE 0 END) AS alta, "
            "COUNT(*) AS total "
            f"FROM interacciones_estres ie {join} {where} "
            "GROUP BY ie.usuario_id "
            "ORDER BY total DESC"
        , params)
        filas = []
        for r in await cur.fetchall():
            d = dict(r)
            total = d["total"] or 0
            # Indice de carga 0-100 (baja=0, media=50, alta=100).
            d["indice_carga"] = round(
                100.0 * (0.0 * (d["baja"] or 0) + 0.5 * (d["media"] or 0) + 1.0 * (d["alta"] or 0)) / total, 1
            ) if total else 0.0
            d["pct_alta"] = _pct(d["alta"], total)
            d["pct_media"] = _pct(d["media"], total)
            d["pct_baja"] = _pct(d["baja"], total)
            filas.append(d)
        return filas


# ===============================================================
# ADMIN: GESTION DE LA CARTA (platos y categorias)
# ===============================================================
async def obtener_menu_admin() -> list[dict]:
    """Todos los platos (disponibles o no) con su categoria, para el admin."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT p.id, p.nombre, p.descripcion, p.precio, p.disponible, "
            "p.categoria_id, c.nombre AS categoria "
            "FROM platos p JOIN categorias c ON c.id = p.categoria_id "
            "ORDER BY c.orden, p.nombre"
        )
        return [dict(r) for r in await cur.fetchall()]


async def listar_categorias() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, nombre, orden FROM categorias ORDER BY orden, nombre")
        return [dict(r) for r in await cur.fetchall()]


async def obtener_o_crear_categoria(nombre: str) -> int:
    nombre = (nombre or "").strip() or "Otros"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM categorias WHERE lower(nombre) = lower(?)", (nombre,))
        row = await cur.fetchone()
        if row:
            return row[0]
        cur = await db.execute("SELECT COALESCE(MAX(orden), 0) + 1 FROM categorias")
        orden = (await cur.fetchone())[0]
        cur = await db.execute("INSERT INTO categorias (nombre, orden) VALUES (?, ?)", (nombre, orden))
        await db.commit()
        return cur.lastrowid


async def crear_plato(nombre, descripcion, precio, categoria_id, disponible=1) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO platos (nombre, descripcion, precio, categoria_id, disponible) "
            "VALUES (?, ?, ?, ?, ?)",
            (nombre.strip(), (descripcion or "").strip(), float(precio), int(categoria_id), int(disponible)),
        )
        await db.commit()
        return cur.lastrowid


async def actualizar_plato(plato_id, nombre, descripcion, precio, categoria_id, disponible) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE platos SET nombre = ?, descripcion = ?, precio = ?, categoria_id = ?, disponible = ? "
            "WHERE id = ?",
            (nombre.strip(), (descripcion or "").strip(), float(precio), int(categoria_id),
             int(disponible), int(plato_id)),
        )
        await db.commit()


async def eliminar_plato(plato_id: int) -> str:
    """Elimina un plato. Si ya tiene pedidos historicos, no se borra fisico
    (romperia la integridad): se DESHABILITA (disponible=0). Si no tiene
    historial, se elimina de verdad junto con su receta."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        cur = await db.execute("SELECT COUNT(*) FROM detalle_pedido WHERE plato_id = ?", (plato_id,))
        tiene_historial = (await cur.fetchone())[0] > 0
        if tiene_historial:
            await db.execute("UPDATE platos SET disponible = 0 WHERE id = ?", (plato_id,))
            await db.commit()
            return "deshabilitado"
        await db.execute("DELETE FROM receta_insumo WHERE plato_id = ?", (plato_id,))
        await db.execute("DELETE FROM platos WHERE id = ?", (plato_id,))
        await db.commit()
        return "eliminado"


async def upsert_plato_por_nombre(nombre, descripcion, precio, categoria_nombre, disponible=1) -> str:
    """Para la importacion: si el plato existe (por nombre) lo actualiza; si
    no, lo crea. La categoria se busca o se crea por nombre."""
    cat_id = await obtener_o_crear_categoria(categoria_nombre)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM platos WHERE lower(nombre) = lower(?)", (nombre.strip(),))
        row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE platos SET descripcion = ?, precio = ?, categoria_id = ?, disponible = ? WHERE id = ?",
                ((descripcion or "").strip(), float(precio), cat_id, int(disponible), row[0]),
            )
            await db.commit()
            return "actualizado"
        await db.execute(
            "INSERT INTO platos (nombre, descripcion, precio, categoria_id, disponible) VALUES (?, ?, ?, ?, ?)",
            (nombre.strip(), (descripcion or "").strip(), float(precio), cat_id, int(disponible)),
        )
        await db.commit()
        return "creado"


# ===============================================================
# ADMIN: GESTION DE USUARIOS (delegar permisos)
# ===============================================================
ROLES_VALIDOS = ("admin", "mesero", "cocinero", "almacenero", "cajero")


async def listar_usuarios_admin() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, nombre, username, rol, activo FROM usuarios ORDER BY rol, nombre"
        )
        return [dict(r) for r in await cur.fetchall()]


async def username_existe(username: str, excluir_id: int | None = None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        if excluir_id:
            cur = await db.execute(
                "SELECT 1 FROM usuarios WHERE lower(username) = lower(?) AND id != ?", (username, excluir_id)
            )
        else:
            cur = await db.execute("SELECT 1 FROM usuarios WHERE lower(username) = lower(?)", (username,))
        return (await cur.fetchone()) is not None


async def crear_usuario(nombre, username, password, rol) -> dict:
    import auth
    username = (username or "").strip()
    if rol not in ROLES_VALIDOS:
        return {"ok": False, "error": f"Rol invalido: {rol}"}
    if not username or not password or not nombre:
        return {"ok": False, "error": "Faltan datos (nombre, usuario y contrasena son obligatorios)."}
    if await username_existe(username):
        return {"ok": False, "error": f"El usuario '{username}' ya existe."}
    jid = f"{username}@127.0.0.1"
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cur = await db.execute(
                "INSERT INTO usuarios (nombre, jid, rol, username, password_hash, activo) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (nombre.strip(), jid, rol, username, auth.hashear(password)),
            )
            await db.commit()
            return {"ok": True, "id": cur.lastrowid}
        except Exception as e:
            return {"ok": False, "error": str(e)}


async def actualizar_usuario(usuario_id, nombre=None, rol=None, activo=None) -> dict:
    if rol is not None and rol not in ROLES_VALIDOS:
        return {"ok": False, "error": f"Rol invalido: {rol}"}
    campos, valores = [], []
    if nombre is not None:
        campos.append("nombre = ?"); valores.append(nombre.strip())
    if rol is not None:
        campos.append("rol = ?"); valores.append(rol)
    if activo is not None:
        campos.append("activo = ?"); valores.append(1 if activo else 0)
    if not campos:
        return {"ok": False, "error": "Nada que actualizar."}
    valores.append(int(usuario_id))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE usuarios SET {', '.join(campos)} WHERE id = ?", valores)
        await db.commit()
    return {"ok": True}


async def cambiar_password(usuario_id, nueva) -> dict:
    import auth
    if not nueva:
        return {"ok": False, "error": "La contrasena no puede estar vacia."}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE usuarios SET password_hash = ? WHERE id = ?",
                         (auth.hashear(nueva), int(usuario_id)))
        await db.commit()
    return {"ok": True}


async def cambiar_mi_password(usuario_id, actual, nueva) -> dict:
    """Cambia la propia contrasena: exige la contrasena ACTUAL correcta."""
    import auth
    if not nueva:
        return {"ok": False, "error": "La nueva contrasena no puede estar vacia."}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT password_hash FROM usuarios WHERE id = ?", (int(usuario_id),))
        row = await cur.fetchone()
        if not row or not auth.verificar(actual, row["password_hash"]):
            return {"ok": False, "error": "La contrasena actual es incorrecta."}
        await db.execute("UPDATE usuarios SET password_hash = ? WHERE id = ?",
                         (auth.hashear(nueva), int(usuario_id)))
        await db.commit()
    return {"ok": True}


# ===============================================================
# ADMIN: GESTION DE INGREDIENTES / INSUMOS
# ===============================================================
async def obtener_insumos_admin() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, nombre, unidad_medida, stock_actual, stock_minimo, fecha_vencimiento, proveedor "
            "FROM insumos ORDER BY nombre"
        )
        return [dict(r) for r in await cur.fetchall()]


async def crear_insumo(nombre, unidad_medida, stock_actual, stock_minimo,
                       fecha_vencimiento=None, proveedor=None) -> dict:
    nombre = (nombre or "").strip()
    if not nombre:
        return {"ok": False, "error": "El nombre es obligatorio."}
    try:
        stock_actual = float(stock_actual or 0)
        stock_minimo = float(stock_minimo or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Stock y minimo deben ser numeros."}
    if stock_actual < 0:
        return {"ok": False, "error": "El stock no puede ser negativo."}
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cur = await db.execute(
                "INSERT INTO insumos (nombre, unidad_medida, stock_actual, stock_minimo, fecha_vencimiento, proveedor) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (nombre, (unidad_medida or "unidad").strip(), stock_actual, stock_minimo,
                 (fecha_vencimiento or None), (proveedor or None)),
            )
            await db.commit()
            return {"ok": True, "id": cur.lastrowid}
        except Exception as e:
            msg = "Ya existe un insumo con ese nombre." if "UNIQUE" in str(e).upper() else str(e)
            return {"ok": False, "error": msg}


async def actualizar_insumo(insumo_id, nombre, unidad_medida, stock_actual, stock_minimo,
                            fecha_vencimiento=None, proveedor=None) -> dict:
    try:
        stock_actual = float(stock_actual or 0)
        stock_minimo = float(stock_minimo or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Stock y minimo deben ser numeros."}
    if stock_actual < 0:
        return {"ok": False, "error": "El stock no puede ser negativo."}
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "UPDATE insumos SET nombre = ?, unidad_medida = ?, stock_actual = ?, stock_minimo = ?, "
                "fecha_vencimiento = ?, proveedor = ? WHERE id = ?",
                ((nombre or "").strip(), (unidad_medida or "unidad").strip(), stock_actual, stock_minimo,
                 (fecha_vencimiento or None), (proveedor or None), int(insumo_id)),
            )
            await db.commit()
            return {"ok": True}
        except Exception as e:
            msg = "Ya existe un insumo con ese nombre." if "UNIQUE" in str(e).upper() else str(e)
            return {"ok": False, "error": msg}


async def eliminar_insumo(insumo_id: int) -> dict:
    """Elimina un insumo solo si NO se usa en la receta de ningun plato
    (la BD lo restringe con ON DELETE RESTRICT). Si esta en uso, avisa."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM receta_insumo WHERE insumo_id = ?", (insumo_id,))
        usos = (await cur.fetchone())[0]
        if usos > 0:
            return {"ok": False, "error": f"No se puede eliminar: se usa en la receta de {usos} plato(s)."}
        await db.execute("DELETE FROM insumos WHERE id = ?", (insumo_id,))
        await db.commit()
        return {"ok": True}


# ===============================================================
# ADMIN: RECETA DE UN PLATO (que insumos usa y en que cantidad)
# La receta alimenta 2 cosas: el descuento de stock al completar el pedido
# (trigger trg_descuenta_stock) y las respuestas de alergenos del RAG.
# ===============================================================
async def obtener_receta(plato_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ri.insumo_id, i.nombre, i.unidad_medida, ri.cantidad_usada "
            "FROM receta_insumo ri JOIN insumos i ON i.id = ri.insumo_id "
            "WHERE ri.plato_id = ? ORDER BY i.nombre",
            (plato_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def guardar_receta(plato_id: int, items: list[dict]) -> dict:
    """Reemplaza por completo la receta del plato. items: lista de
    {insumo_id, cantidad}. Se ignoran cantidades <= 0 y se deduplica por
    insumo (gana la ultima), porque receta_insumo es UNIQUE(plato, insumo)."""
    limpios = {}
    for it in items:
        try:
            iid = int(it["insumo_id"])
            cant = float(it["cantidad"])
        except (KeyError, TypeError, ValueError):
            continue
        if cant > 0:
            limpios[iid] = cant  # dedup: la ultima cantidad para ese insumo
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM receta_insumo WHERE plato_id = ?", (plato_id,))
        for iid, cant in limpios.items():
            await db.execute(
                "INSERT INTO receta_insumo (plato_id, insumo_id, cantidad_usada) VALUES (?, ?, ?)",
                (plato_id, iid, cant),
            )
        await db.commit()
    return {"ok": True, "items": len(limpios)}