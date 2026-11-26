"""
inventario_predictivo.py
------------------------
INVENTARIO PREDICTIVO: pronostica la demanda de los proximos dias y la
traduce a insumos usando las recetas, para anticipar compras y mermas.

Metodo (explicable y sin librerias externas):
  1. Historial de pedidos de las ultimas SEMANAS_HISTORIA semanas, agrupado
     por plato y dia de la semana. Solo cuentan los dias con operacion.
  2. Promedio movil PONDERADO por dia de la semana: las semanas mas recientes
     pesan mas (pesos 1..k). Asi captura que viernes y sabado venden mas.
  3. Demanda de platos -> consumo de insumos (tabla receta_insumo).
  4. Por insumo: consumo diario esperado, dias de cobertura con el stock
     actual, fecha estimada de agotamiento y compra sugerida
     (consumo del horizonte + stock minimo de seguridad - stock actual).
  5. Vencimientos: insumos vencidos y riesgo de merma (lo que vencera antes
     de poder consumirse).
"""
import math
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import aiosqlite

import db_utils as db
import monitor

SEMANAS_HISTORIA = 6
DIAS_POR_VENCER = 5
COBERTURA_URGENTE_DIAS = 3      # se agota en menos de 3 dias: compra urgente
DIAS_SEMANA = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]
_ultimo_aviso_agente = 0.0


def _redondear_compra(cantidad: float, unidad: str) -> float:
    if cantidad <= 0:
        return 0
    if (unidad or "").lower().startswith("unid"):
        return math.ceil(cantidad)
    return math.ceil(cantidad * 2) / 2      # kg / lt: de medio en medio


def _fecha(valor):
    try:
        return datetime.strptime(str(valor)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


async def _leer_datos(desde: date, hasta: date):
    async with aiosqlite.connect(db.DB_PATH) as con:
        ventas = await (await con.execute(
            "SELECT date(p.fecha_hora), dp.plato_id, SUM(dp.cantidad) "
            "FROM detalle_pedido dp JOIN pedidos p ON p.id = dp.pedido_id "
            "WHERE date(p.fecha_hora) >= ? AND date(p.fecha_hora) < ? "
            "AND (p.estado != 'cancelado' OR p.motivo_anulacion IS NOT NULL) "
            "GROUP BY date(p.fecha_hora), dp.plato_id",
            (desde.isoformat(), hasta.isoformat()))).fetchall()
        platos = await (await con.execute(
            "SELECT id, nombre FROM platos WHERE disponible = 1")).fetchall()
        recetas = await (await con.execute(
            "SELECT plato_id, insumo_id, cantidad_usada FROM receta_insumo")).fetchall()
        insumos = await (await con.execute(
            "SELECT id, nombre, unidad_medida, stock_actual, stock_minimo, fecha_vencimiento "
            "FROM insumos ORDER BY nombre")).fetchall()
    return ventas, platos, recetas, insumos


async def pronosticar(dias: int = 7, semanas: int = SEMANAS_HISTORIA) -> dict:
    dias = max(1, min(int(dias), 30))
    hoy = date.today()
    ventas_raw, platos_raw, recetas_raw, insumos_raw = await _leer_datos(
        hoy - timedelta(weeks=semanas), hoy)

    platos = {pid: nombre for pid, nombre in platos_raw}
    ventas = defaultdict(lambda: defaultdict(float))      # dia -> plato -> cantidad
    for dia_txt, plato_id, cantidad in ventas_raw:
        d = _fecha(dia_txt)
        if d:
            ventas[d][plato_id] += cantidad
    dias_operacion = sorted(ventas)
    por_dia_semana = defaultdict(list)
    for d in dias_operacion:
        por_dia_semana[d.weekday()].append(d)

    def demanda(plato_id: int, dia_semana: int) -> float:
        ocurrencias = por_dia_semana.get(dia_semana) or dias_operacion   # respaldo: todos los dias
        ocurrencias = ocurrencias[-semanas:]
        if not ocurrencias:
            return 0.0
        pesos = range(1, len(ocurrencias) + 1)                            # recientes pesan mas
        return sum(w * ventas[d].get(plato_id, 0) for w, d in zip(pesos, ocurrencias)) / sum(pesos)

    # ---- pronostico de platos por dia ----
    horizonte = [hoy + timedelta(days=i) for i in range(dias)]
    pron_platos = {d: {pid: demanda(pid, d.weekday()) for pid in platos} for d in horizonte}

    # ---- consumo de insumos por dia (via recetas) ----
    receta = defaultdict(list)
    for plato_id, insumo_id, cant in recetas_raw:
        receta[plato_id].append((insumo_id, cant))
    consumo = {d: defaultdict(float) for d in horizonte}
    for d in horizonte:
        for pid, cant_plato in pron_platos[d].items():
            for insumo_id, cant_ins in receta.get(pid, []):
                consumo[d][insumo_id] += cant_plato * cant_ins

    insumos, compras, vencidos, riesgo_merma = [], [], [], []
    for iid, nombre, unidad, stock, minimo, vence in insumos_raw:
        stock, minimo = float(stock or 0), float(minimo or 0)
        por_dia = [consumo[d].get(iid, 0.0) for d in horizonte]
        total = sum(por_dia)
        diario = total / dias

        cobertura, agota = None, None
        if diario > 0:
            restante = stock
            for d, c in zip(horizonte, por_dia):
                restante -= c
                if restante < 0:
                    agota = d
                    break
            cobertura = round(stock / diario, 1)
            if agota is None:
                agota = hoy + timedelta(days=int(stock / diario))

        sugerido = _redondear_compra(total + minimo - stock, unidad)
        if stock <= minimo or (cobertura is not None and cobertura < COBERTURA_URGENTE_DIAS):
            prioridad = "urgente"
        elif sugerido > 0:
            prioridad = "pronto"
        else:
            prioridad = "ok"

        fv = _fecha(vence)
        dias_para_vencer = (fv - hoy).days if fv else None
        estado_venc, merma = None, 0.0
        if fv and dias_para_vencer < 0:
            estado_venc = "vencido"
            vencidos.append({"nombre": nombre, "fecha_vencimiento": vence, "stock_actual": round(stock, 2),
                             "unidad": unidad,
                             "compra_si_baja": _redondear_compra(total + minimo, unidad)})
        elif fv:
            if dias_para_vencer <= DIAS_POR_VENCER:
                estado_venc = "por_vencer"
            hasta_vencer = sum(c for d, c in zip(horizonte, por_dia) if d <= fv)
            if fv > horizonte[-1]:
                hasta_vencer += diario * (fv - horizonte[-1]).days
            merma = round(max(0.0, stock - hasta_vencer), 2)
            if merma > 0 and dias_para_vencer <= dias:
                riesgo_merma.append({"nombre": nombre, "fecha_vencimiento": vence, "dias": dias_para_vencer,
                                     "merma_estimada": merma, "unidad": unidad})

        fila = {
            "nombre": nombre, "unidad": unidad, "stock_actual": round(stock, 2), "stock_minimo": minimo,
            "consumo_diario": round(diario, 2), "consumo_horizonte": round(total, 2),
            "cobertura_dias": cobertura, "se_agota": agota.isoformat() if agota else None,
            "compra_sugerida": sugerido, "prioridad": prioridad,
            "fecha_vencimiento": vence, "estado_vencimiento": estado_venc,
        }
        insumos.append(fila)
        if prioridad != "ok":
            compras.append(fila)

    orden = {"urgente": 0, "pronto": 1, "ok": 2}
    compras.sort(key=lambda f: (orden[f["prioridad"]], f["cobertura_dias"] if f["cobertura_dias"] is not None else 1e9))

    diario_platos = []
    for d in horizonte:
        total = sum(pron_platos[d].values())
        top = sorted(((platos[p], q) for p, q in pron_platos[d].items() if q > 0), key=lambda x: -x[1])[:3]
        diario_platos.append({"fecha": d.isoformat(), "dia": DIAS_SEMANA[d.weekday()],
                              "platos": round(total), "top": [{"nombre": n, "cantidad": round(q, 1)} for n, q in top]})
    totales = defaultdict(float)
    for d in horizonte:
        for p, q in pron_platos[d].items():
            totales[platos[p]] += q
    top_platos = [{"nombre": n, "cantidad": round(q)} for n, q in sorted(totales.items(), key=lambda x: -x[1])[:5] if q > 0]

    urgentes = [c for c in compras if c["prioridad"] == "urgente"]
    con_cob = [i for i in insumos if i["cobertura_dias"] is not None]
    menor = min(con_cob, key=lambda i: i["cobertura_dias"]) if con_cob else None
    _avisar_agente(len(urgentes), len(compras), dias)

    return {
        "generado": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "horizonte_dias": dias,
        "metodo": (f"Promedio movil ponderado por dia de la semana (ultimas {semanas} semanas, "
                   f"{len(dias_operacion)} dias con ventas); demanda convertida a insumos con las recetas."),
        "dias_con_historia": len(dias_operacion),
        "resumen": {
            "platos_estimados": round(sum(x["platos"] for x in diario_platos)),
            "insumos_a_comprar": len(compras), "urgentes": len(urgentes),
            "menor_cobertura": {"nombre": menor["nombre"], "dias": menor["cobertura_dias"]} if menor else None,
            "vencidos": len(vencidos), "riesgo_merma": len(riesgo_merma),
        },
        "pronostico_diario": diario_platos,
        "top_platos": top_platos,
        "compras": compras,
        "insumos": insumos,
        "vencidos": vencidos,
        "riesgo_merma": riesgo_merma,
    }


def _avisar_agente(urgentes: int, a_comprar: int, dias: int):
    """El agente Almacenero deja constancia del pronostico (como mucho 1 vez por hora)."""
    global _ultimo_aviso_agente
    if time.time() - _ultimo_aviso_agente < 3600:
        return
    _ultimo_aviso_agente = time.time()
    monitor.registrar_actividad_agente(
        "almacenero", "Genero el pronostico de demanda de insumos",
        f"{a_comprar} insumos requieren compra en {dias} dias ({urgentes} urgentes)")
