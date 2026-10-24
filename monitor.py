"""
monitor.py
------------
Modulo de TELEMETRIA y OBSERVABILIDAD del sistema multiagente.

Inspirado en el modelo de plataformas profesionales (Datadog, New Relic),
recolecta cuatro familias de senales:

  1. TRAZAS HTTP      - cada peticion: ruta, metodo, status, latencia.
  2. ERRORES          - separados en dos clases, que es la distincion
                        clave para diagnosticar:
                          * FUNCIONALES  (4xx): el usuario pidio algo
                            invalido. Ej: plato inexistente, pedido ya
                            cerrado. El sistema esta sano; el uso no.
                          * NO FUNCIONALES (5xx): el sistema fallo. Ej:
                            Ollama caido, timeout del LLM, error de BD.
                            Aqui hay que intervenir.
  3. SALUD DE AGENTES - vivo/caido, reinicios (viene de agent_runtime).
  4. RECURSOS         - CPU y memoria del proceso (via psutil).

Todo se guarda en memoria (ventanas deslizantes, sin costo de disco) y
los eventos relevantes se persisten en SQLite para analisis posterior.

Las latencias se resumen con percentiles (p50, p95, p99) en lugar de
promedios: el promedio esconde los casos lentos, que son justamente los
que percibe el trabajador y los que impactan la carga temporal medida
en la tesis.
"""
import time
import statistics
from collections import deque, Counter, defaultdict
from datetime import datetime, timedelta

try:
    import psutil
    _proceso = psutil.Process()
    _PSUTIL = True
except Exception:
    _PSUTIL = False
    _proceso = None

# ---------------------------------------------------------------
# Configuracion de ventanas
# ---------------------------------------------------------------
MAX_TRAZAS = 2000          # ultimas N peticiones HTTP en memoria
MAX_EVENTOS = 300          # ultimos N eventos del sistema (log)
VENTANA_SERIE_MIN = 30     # minutos de la serie temporal

# Rutas que no queremos ensuciar las metricas (el propio monitor)
RUTAS_IGNORADAS = ("/api/monitor", "/metrics", "/static", "/favicon.ico")

# ---------------------------------------------------------------
# UMBRALES CONFIGURABLES
# Si una metrica supera su umbral, se genera una alerta automatica.
# Los valores se pueden modificar en tiempo real via /api/monitor/umbrales.
# ---------------------------------------------------------------
_umbrales = {
    "latencia_http_ms": 3000,       # p95 HTTP: alerta si supera 3 segundos
    "latencia_llm_ms": 15000,       # p95 LLM: alerta si supera 15 segundos
    "tasa_error_pct": 10.0,         # tasa de error total: alerta si supera 10%
    "tasa_5xx_pct": 2.0,            # tasa de errores no funcionales: alerta si supera 2%
}
_alertas_activas = {}    # clave -> timestamp de cuando se activo (para no repetir)
_COOLDOWN_ALERTA = 60    # segundos entre alertas repetidas de la misma clave

# ---------------------------------------------------------------
# PLAN DE CONTINGENCIA (AUTO-REMEDIACION)
# Igual que el supervisor de agentes reinicia los agentes caidos, el monitor
# ejecuta una ACCION DE MITIGACION cuando una alerta llega a estado CRITICO
# (valor > 2x umbral). Las acciones concretas las registra app_principal
# (porque necesitan tocar el motor LLM); aqui solo se orquestan, con su
# propio cooldown y un historial de lo ejecutado, visible en el panel.
# ---------------------------------------------------------------
import threading

_acciones_contingencia = {}       # clave_alerta -> {"descripcion": str, "func": callable}
_historial_contingencia = deque(maxlen=100)
_ultima_contingencia = {}         # clave -> timestamp de la ultima ejecucion
_COOLDOWN_CONTINGENCIA = 120      # segundos entre remediaciones de la misma alerta

_ultimo_aviso_llm = 0.0           # throttle del warning "el LLM tardo Xs en responder"

# ---------------------------------------------------------------
# Estado en memoria
# ---------------------------------------------------------------
_trazas = deque(maxlen=MAX_TRAZAS)
_eventos = deque(maxlen=MAX_EVENTOS)
_latencias_llm = deque(maxlen=200)
_actividad_agentes = deque(maxlen=300)   # comportamiento de cada agente (para el panel admin)
_arranque = time.time()

_contadores = Counter()          # total, ok, error_funcional, error_no_funcional
_codigos = Counter()             # conteo por codigo HTTP exacto (200, 404, 500...)
_por_ruta = defaultdict(lambda: {"peticiones": 0, "errores": 0, "latencias": deque(maxlen=200)})
_serie_minutos = defaultdict(lambda: {"ok": 0, "err4xx": 0, "err5xx": 0, "latencias": []})


# Significado legible de los codigos HTTP mas frecuentes en este sistema.
SIGNIFICADO_CODIGOS = {
    200: "OK",
    201: "Creado",
    204: "Sin contenido",
    304: "No modificado (cache)",
    400: "Peticion mal formada",
    401: "No autenticado",
    403: "Prohibido",
    404: "No encontrado",
    405: "Metodo no permitido",
    422: "Datos invalidos",
    429: "Demasiadas peticiones",
    500: "Error interno del servidor",
    502: "Puerta de enlace incorrecta",
    503: "Servicio no disponible",
    504: "Tiempo de espera agotado",
}


def _clave_minuto(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def clasificar(status: int) -> str:
    """Clasifica un status HTTP en las tres categorias del dashboard."""
    if status >= 500:
        return "no_funcional"     # el sistema fallo
    if status >= 400:
        return "funcional"        # el uso fue invalido
    return "ok"


# ---------------------------------------------------------------
# Registro de senales
# ---------------------------------------------------------------
def registrar_peticion(ruta: str, metodo: str, status: int, ms: float):
    if any(ruta.startswith(r) for r in RUTAS_IGNORADAS):
        return

    ts = time.time()
    clase = clasificar(status)

    _trazas.append({
        "ts": ts,
        "hora": datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
        "ruta": ruta,
        "metodo": metodo,
        "status": status,
        "ms": round(ms, 1),
        "clase": clase,
    })

    _contadores["total"] += 1
    _contadores[clase] += 1
    _codigos[status] += 1

    r = _por_ruta[f"{metodo} {ruta}"]
    r["peticiones"] += 1
    r["latencias"].append(ms)
    if clase != "ok":
        r["errores"] += 1

    m = _serie_minutos[_clave_minuto(ts)]
    m["latencias"].append(ms)
    if clase == "ok":
        m["ok"] += 1
    elif clase == "funcional":
        m["err4xx"] += 1
    else:
        m["err5xx"] += 1

    _podar_serie()

    if clase == "no_funcional":
        registrar_evento("error", f"{metodo} {ruta} devolvio {status}", origen="http")


def registrar_evento(nivel: str, mensaje: str, origen: str = "sistema"):
    """nivel: info | warning | error"""
    _eventos.appendleft({
        "ts": time.time(),
        "hora": datetime.now().strftime("%H:%M:%S"),
        "nivel": nivel,
        "origen": origen,
        "mensaje": mensaje,
    })


def registrar_actividad_agente(agente: str, accion: str, detalle: str = ""):
    """Registra una accion realizada por un agente SPADE, para que el panel
    admin muestre el COMPORTAMIENTO de cada agente en tiempo real."""
    _actividad_agentes.appendleft({
        "ts": time.time(),
        "hora": datetime.now().strftime("%H:%M:%S"),
        "agente": agente,
        "accion": accion,
        "detalle": detalle,
    })


def actividad_agentes(n: int = 80, agente: str | None = None) -> list:
    datos = list(_actividad_agentes)
    if agente:
        datos = [a for a in datos if a["agente"] == agente]
    return datos[:n]


def registrar_latencia_llm(ms: float, ok: bool = True):
    global _ultimo_aviso_llm
    _latencias_llm.append({"ms": ms, "ok": ok, "ts": time.time()})
    if not ok:
        # Los fallos SIEMPRE se registran (son criticos, no hay throttle).
        registrar_evento("error", f"Fallo la invocacion al LLM ({ms:.0f} ms)", origen="ollama")
    elif ms > 8000:
        # Con latencias altas, cada llamada generaba un warning y saturaba el
        # log. Throttle: un aviso cada 30s como maximo (la metrica p95 sigue
        # capturando todas las muestras; esto solo limita el ruido del log).
        ahora = time.time()
        if ahora - _ultimo_aviso_llm > 30:
            _ultimo_aviso_llm = ahora
            registrar_evento("warning", f"El LLM tardo {ms/1000:.1f}s en responder", origen="ollama")


def _podar_serie():
    limite = datetime.now() - timedelta(minutes=VENTANA_SERIE_MIN)
    corte = limite.strftime("%H:%M")
    # Comparacion textual de HH:MM funciona salvo cruce de medianoche;
    # si se cruza, se limpia la serie entera (aceptable: es una ventana viva).
    if len(_serie_minutos) > VENTANA_SERIE_MIN * 2:
        for k in [k for k in _serie_minutos if k < corte]:
            _serie_minutos.pop(k, None)


# ---------------------------------------------------------------
# Calculo de percentiles
# ---------------------------------------------------------------
def _percentil(valores, p: float) -> float:
    if not valores:
        return 0.0
    datos = sorted(valores)
    k = (len(datos) - 1) * p
    f = int(k)
    c = min(f + 1, len(datos) - 1)
    if f == c:
        return round(datos[f], 1)
    return round(datos[f] + (datos[c] - datos[f]) * (k - f), 1)


def _resumen_latencias(valores) -> dict:
    v = list(valores)
    if not v:
        return {"p50": 0, "p95": 0, "p99": 0, "media": 0, "max": 0, "muestras": 0}
    return {
        "p50": _percentil(v, 0.50),
        "p95": _percentil(v, 0.95),
        "p99": _percentil(v, 0.99),
        "media": round(statistics.fmean(v), 1),
        "max": round(max(v), 1),
        "muestras": len(v),
    }


# ---------------------------------------------------------------
# Consultas para el dashboard
# ---------------------------------------------------------------
def resumen(estado_agentes: dict | None = None) -> dict:
    total = _contadores["total"]
    ok = _contadores["ok"]
    f4 = _contadores["funcional"]
    f5 = _contadores["no_funcional"]

    latencias = [t["ms"] for t in _trazas]
    lat = _resumen_latencias(latencias)

    llm_ms = [x["ms"] for x in _latencias_llm]
    llm_fallos = sum(1 for x in _latencias_llm if not x["ok"])

    tasa_error = round((f4 + f5) / total * 100, 2) if total else 0.0
    tasa_5xx = round(f5 / total * 100, 2) if total else 0.0

    # Semaforo de salud global (como el "Apdex" de las plataformas APM)
    if f5 > 0 or (estado_agentes and any(not a["vivo"] for a in estado_agentes.values())):
        estado = "critico"
    elif tasa_error > 10 or lat["p95"] > 5000:
        estado = "degradado"
    else:
        estado = "saludable"

    recursos = {"cpu": 0.0, "memoria_mb": 0.0}
    if _PSUTIL:
        try:
            recursos = {
                "cpu": round(_proceso.cpu_percent(interval=None), 1),
                "memoria_mb": round(_proceso.memory_info().rss / (1024 * 1024), 1),
            }
        except Exception:
            pass

    return {
        "estado": estado,
        "uptime_seg": int(time.time() - _arranque),
        "peticiones": {"total": total, "ok": ok, "err_funcional": f4, "err_no_funcional": f5},
        "tasa_error_pct": tasa_error,
        "tasa_5xx_pct": tasa_5xx,
        "latencia_http": lat,
        "latencia_llm": {**_resumen_latencias(llm_ms), "fallos": llm_fallos},
        "recursos": recursos,
        "agentes": estado_agentes or {},
    }


def serie_temporal() -> dict:
    claves = sorted(_serie_minutos.keys())[-VENTANA_SERIE_MIN:]
    return {
        "etiquetas": claves,
        "ok": [_serie_minutos[k]["ok"] for k in claves],
        "err4xx": [_serie_minutos[k]["err4xx"] for k in claves],
        "err5xx": [_serie_minutos[k]["err5xx"] for k in claves],
        "p95": [_percentil(_serie_minutos[k]["latencias"], 0.95) for k in claves],
    }


def distribucion_codigos() -> dict:
    """Distribucion de codigos HTTP, para el grafico de pastel.
    Devuelve el detalle por codigo exacto y el agregado por familia."""
    familias = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
    detalle = []
    for codigo, n in sorted(_codigos.items()):
        fam = f"{codigo // 100}xx"
        if fam in familias:
            familias[fam] += n
        detalle.append({
            "codigo": codigo,
            "cantidad": n,
            "familia": fam,
            "clase": clasificar(codigo),
            "significado": SIGNIFICADO_CODIGOS.get(codigo, "Otro"),
        })
    detalle.sort(key=lambda x: x["cantidad"], reverse=True)
    total = sum(familias.values())
    return {
        "familias": familias,
        "detalle": detalle,
        "total": total,
    }


def top_rutas(n: int = 8) -> list:
    filas = []
    for ruta, d in _por_ruta.items():
        lat = _resumen_latencias(d["latencias"])
        filas.append({
            "ruta": ruta,
            "peticiones": d["peticiones"],
            "errores": d["errores"],
            "tasa_error": round(d["errores"] / d["peticiones"] * 100, 1) if d["peticiones"] else 0.0,
            "p50": lat["p50"],
            "p95": lat["p95"],
        })
    filas.sort(key=lambda x: x["peticiones"], reverse=True)
    return filas[:n]


def errores_recientes(n: int = 25) -> list:
    return [t for t in reversed(_trazas) if t["clase"] != "ok"][:n]


def eventos_recientes(n: int = 30) -> list:
    return list(_eventos)[:n]


def trazas_recientes(n: int = 40) -> list:
    return [dict(t) for t in list(_trazas)[-n:]][::-1]


def reiniciar_metricas():
    """Limpia todas las metricas (util antes de una demo o del experimento)."""
    _trazas.clear()
    _eventos.clear()
    _latencias_llm.clear()
    _contadores.clear()
    _codigos.clear()
    _por_ruta.clear()
    _serie_minutos.clear()
    _alertas_activas.clear()
    _actividad_agentes.clear()
    _historial_contingencia.clear()
    _ultima_contingencia.clear()
    registrar_evento("info", "Metricas reiniciadas manualmente", origen="monitor")


# ---------------------------------------------------------------
# UMBRALES Y ALERTAS
# ---------------------------------------------------------------
def obtener_umbrales() -> dict:
    return dict(_umbrales)


def actualizar_umbrales(nuevos: dict):
    for clave, valor in nuevos.items():
        if clave in _umbrales and isinstance(valor, (int, float)):
            _umbrales[clave] = float(valor)
    registrar_evento("info", f"Umbrales actualizados: {nuevos}", origen="monitor")


def verificar_umbrales():
    """Revisa si alguna metrica supera su umbral y genera alerta. Se llama
    automaticamente en cada peticion; el cooldown evita spam de alertas."""
    ahora = time.time()
    total = _contadores["total"]
    if total < 5:
        return  # no tiene sentido alertar con muy pocas muestras

    latencias_http = [t["ms"] for t in _trazas]
    p95_http = _percentil(latencias_http, 0.95)
    llm_ms = [x["ms"] for x in _latencias_llm]
    p95_llm = _percentil(llm_ms, 0.95)
    f4 = _contadores["funcional"]
    f5 = _contadores["no_funcional"]
    tasa_error = (f4 + f5) / total * 100
    tasa_5xx = f5 / total * 100

    checks = [
        ("latencia_http_ms", p95_http, f"Latencia HTTP p95 ({p95_http:.0f} ms) supera el umbral de {_umbrales['latencia_http_ms']:.0f} ms"),
        ("latencia_llm_ms", p95_llm, f"Latencia LLM p95 ({p95_llm:.0f} ms) supera el umbral de {_umbrales['latencia_llm_ms']:.0f} ms"),
        ("tasa_error_pct", tasa_error, f"Tasa de error ({tasa_error:.1f}%) supera el umbral de {_umbrales['tasa_error_pct']:.1f}%"),
        ("tasa_5xx_pct", tasa_5xx, f"Tasa de errores 5xx ({tasa_5xx:.1f}%) supera el umbral de {_umbrales['tasa_5xx_pct']:.1f}%"),
    ]

    for clave, valor, mensaje in checks:
        if valor > _umbrales[clave]:
            ultimo = _alertas_activas.get(clave, 0)
            if ahora - ultimo > _COOLDOWN_ALERTA:
                _alertas_activas[clave] = ahora
                registrar_evento("warning", f"ALERTA UMBRAL: {mensaje}", origen="monitor")
            # Estado CRITICO (valor > 2x umbral): se dispara el plan de
            # contingencia (con su propio cooldown). Las alertas 'warning'
            # solo avisan; las 'criticas' ademas intentan remediar.
            if valor > _umbrales[clave] * 2:
                _ejecutar_contingencia(clave, valor, _umbrales[clave])
        else:
            _alertas_activas.pop(clave, None)


def alertas_activas() -> list[dict]:
    """Devuelve las alertas actualmente activas (umbrales superados)."""
    total = _contadores["total"]
    if total < 5:
        return []

    latencias_http = [t["ms"] for t in _trazas]
    p95_http = _percentil(latencias_http, 0.95)
    llm_ms = [x["ms"] for x in _latencias_llm]
    p95_llm = _percentil(llm_ms, 0.95)
    f4 = _contadores["funcional"]
    f5 = _contadores["no_funcional"]
    tasa_error = (f4 + f5) / total * 100
    tasa_5xx = f5 / total * 100

    alertas = []
    checks = [
        ("latencia_http_ms", p95_http, "Latencia HTTP p95 alta"),
        ("latencia_llm_ms", p95_llm, "Latencia LLM p95 alta"),
        ("tasa_error_pct", tasa_error, "Tasa de error alta"),
        ("tasa_5xx_pct", tasa_5xx, "Errores no funcionales (5xx) altos"),
    ]
    for clave, valor, titulo in checks:
        umbral = _umbrales[clave]
        if valor > umbral:
            alertas.append({
                "clave": clave,
                "titulo": titulo,
                "valor_actual": round(valor, 1),
                "umbral": umbral,
                "severidad": "critico" if valor > umbral * 2 else "warning",
            })
    return alertas


# ---------------------------------------------------------------
# PLAN DE CONTINGENCIA (AUTO-REMEDIACION)
# ---------------------------------------------------------------
def registrar_accion_contingencia(clave: str, descripcion: str, func):
    """app_principal registra aqui QUE hacer cuando la alerta 'clave' llega a
    critico. 'func(valor, umbral) -> str' devuelve un texto con el resultado."""
    _acciones_contingencia[clave] = {"descripcion": descripcion, "func": func}


def _ejecutar_contingencia(clave: str, valor: float, umbral: float):
    """Dispara la accion de contingencia de 'clave' si hay una registrada y
    ya paso su cooldown. Corre en un HILO aparte para no bloquear la peticion
    HTTP (la remediacion puede tocar el motor LLM)."""
    accion = _acciones_contingencia.get(clave)
    if not accion:
        return
    ahora = time.time()
    if ahora - _ultima_contingencia.get(clave, 0) < _COOLDOWN_CONTINGENCIA:
        return
    _ultima_contingencia[clave] = ahora

    def _correr():
        hora = datetime.now().strftime("%H:%M:%S")
        registrar_evento("warning", f"CONTINGENCIA disparada [{clave}]: {accion['descripcion']}", origen="contingencia")
        entrada = {"hora": hora, "clave": clave, "accion": accion["descripcion"],
                   "valor": round(valor, 1), "umbral": umbral, "ok": False, "resultado": ""}
        try:
            resultado = accion["func"](valor, umbral)
            entrada["ok"] = True
            entrada["resultado"] = resultado or "ejecutado"
            registrar_evento("info", f"CONTINGENCIA OK [{clave}]: {entrada['resultado']}", origen="contingencia")
        except Exception as e:
            entrada["resultado"] = f"error: {e}"
            registrar_evento("error", f"CONTINGENCIA FALLO [{clave}]: {e}", origen="contingencia")
        _historial_contingencia.appendleft(entrada)

    threading.Thread(target=_correr, daemon=True, name=f"contingencia-{clave}").start()


def plan_contingencia() -> list[dict]:
    """Acciones de contingencia registradas (para mostrar el 'plan' en el panel)."""
    etiquetas = {
        "latencia_http_ms": "Latencia HTTP p95 critica",
        "latencia_llm_ms": "Latencia LLM p95 critica",
        "tasa_5xx_pct": "Errores no funcionales (5xx) criticos",
        "tasa_error_pct": "Tasa de error critica",
    }
    return [
        {"clave": clave, "alerta": etiquetas.get(clave, clave), "accion": a["descripcion"]}
        for clave, a in _acciones_contingencia.items()
    ]


def historial_contingencia(n: int = 30) -> list[dict]:
    """Ultimas remediaciones ejecutadas."""
    return list(_historial_contingencia)[:n]