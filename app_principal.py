"""
app_principal.py
------------------
Punto de entrada UNICO del sistema. Levanta los 4 agentes SPADE (en un
hilo dedicado, ver agent_runtime.py) y sirve los 4 paneles web.

IMPORTANTE: corre primero 'spade run --host 127.0.0.1' en otra terminal.
El host DEBE ser 127.0.0.1 (no 'localhost'): los JIDs de los agentes son
cocinero@127.0.0.1, etc., y el dominio del servidor XMPP tiene que coincidir
exactamente, o los agentes fallan al registrarse.

Uso:
    Terminal 1: spade run --host 127.0.0.1
    Terminal 2: python app_principal.py
    Luego abre:
        http://localhost:8000/         (Mesero)
        http://localhost:8000/cocina   (Cocina)
        http://localhost:8000/almacen  (Almacen)
        http://localhost:8000/caja     (Caja)
"""
import os
import asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from langchain_ollama import OllamaLLM
from contextlib import asynccontextmanager

import agent_runtime
import agentes
import monitor
import db_utils as db
import chroma_db
import menu_rag
import export_excel
import import_docs
import auth
from fastapi.responses import RedirectResponse, JSONResponse
from nlp_pedido import detectar_intencion, extraer_items_pedido
from deteccion_estres import analizar_mensaje


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ciclo de vida de la app (reemplaza los @app.on_event, ya deprecados).
    Arranque: migraciones idempotentes, credenciales, recuperacion del pedido
    abierto, indexado del RAG y arranque de los agentes SPADE en un hilo.
    Cierre: detiene los agentes."""
    import aplicar_migracion_v3
    import aplicar_migracion_v4
    import aplicar_migracion_v5
    import aplicar_migracion_v6
    import aplicar_migracion_v7
    aplicar_migracion_v3.main()
    aplicar_migracion_v4.main()
    aplicar_migracion_v5.main()
    aplicar_migracion_v6.main()
    aplicar_migracion_v7.main()
    await db.sembrar_credenciales_demo()
    await db.sembrar_usuarios_demo()

    global pedido_actual_id
    pedido_actual_id = await db.ultimo_pedido_abierto()
    if pedido_actual_id:
        print(f"[app_principal] Pedido activo recuperado tras reinicio: #{pedido_actual_id}")

    try:
        platos_rag = await db.obtener_menu_para_rag()
        indexados = menu_rag.reindexar_menu(platos_rag)
        print(f"[app_principal] Menu indexado en el RAG: {indexados} platos.")
    except Exception as e:
        print(f"[app_principal] Aviso: no se pudo indexar el menu para RAG (Chroma/Ollama '{menu_rag.MODELO_EMBEDDINGS}'?): {e}")

    await asyncio.to_thread(agent_runtime.iniciar)

    yield

    agent_runtime.detener()


app = FastAPI(lifespan=lifespan)


# ==================================================================
# MIDDLEWARE DE TELEMETRIA
# Intercepta TODA peticion HTTP y mide: ruta, metodo, status y latencia.
# Es el equivalente al "APM agent" de plataformas como Datadog.
# ==================================================================
@app.middleware("http")
async def telemetria(request, call_next):
    import time as _t
    inicio = _t.perf_counter()
    try:
        respuesta = await call_next(request)
        status = respuesta.status_code
    except Exception as e:
        # Excepcion no controlada: se cuenta como error NO FUNCIONAL (5xx)
        ms = (_t.perf_counter() - inicio) * 1000
        monitor.registrar_peticion(request.url.path, request.method, 500, ms)
        monitor.registrar_evento("error", f"Excepcion no controlada: {e}", origen="app")
        raise
    ms = (_t.perf_counter() - inicio) * 1000
    monitor.registrar_peticion(request.url.path, request.method, status, ms)
    monitor.verificar_umbrales()
    return respuesta


# ==================================================================
# GATE DE AUTENTICACION
# Las APIs de datos y el chat exigen sesion iniciada (cierra el acceso
# directo sin login). Quedan abiertas: login/logout/sesion, /metrics (para
# Grafana) y /static. Las paginas HTML ya se protegen en sus handlers.
# ==================================================================
_RUTAS_ABIERTAS = {"/api/session", "/login", "/logout", "/metrics", "/favicon.ico"}


def _requiere_sesion(path: str) -> bool:
    if path in _RUTAS_ABIERTAS:
        return False
    return path.startswith("/api/") or path == "/chat"


@app.middleware("http")
async def gate_auth(request, call_next):
    path = request.url.path
    if _requiere_sesion(path) and not auth.obtener_sesion(request.cookies.get("sesion")):
        return JSONResponse(status_code=401, content={"ok": False, "error": "No autenticado.", "login": True})
    return await call_next(request)


ruta_base = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(ruta_base, "templates"))

# Carpeta opcional para imagenes (logo de DonOso, etc). Si no existe, se crea
# vacia y los paneles usan el icono por defecto sin romperse.
from fastapi.staticfiles import StaticFiles
ruta_static = os.path.join(ruta_base, "static")
os.makedirs(ruta_static, exist_ok=True)
app.mount("/static", StaticFiles(directory=ruta_static), name="static")

MODELO_PRINCIPAL = "llama3"
MODELO_LIVIANO = "llama3.2:3b"   # modelo de respaldo para la degradacion elegante

llm_global = OllamaLLM(model=MODELO_PRINCIPAL, keep_alive="1h")
# Para mas velocidad, descarga el modelo liviano con: ollama pull llama3.2:3b
# El plan de contingencia degrada automaticamente a ese modelo si la latencia
# LLM se vuelve critica de forma sostenida (ver acciones registradas abajo).


def modelo_actual() -> str:
    """Nombre del modelo LLM que se esta usando ahora mismo."""
    return getattr(llm_global, "model", MODELO_PRINCIPAL)


# ==================================================================
# PLAN DE CONTINGENCIA (acciones de auto-remediacion)
# Se registran en el monitor; este las dispara cuando una alerta llega a
# estado CRITICO. Analogas al supervisor que reinicia agentes caidos, pero
# aplicadas al motor de inferencia (el cuello de botella real del sistema).
# ==================================================================
_escalada_llm = {"n": 0}


def _reiniciar_motor_llm() -> str:
    """Reinstancia el cliente de Ollama y renueva keep_alive. Mitiga la
    latencia causada por la descarga del modelo de la RAM entre peticiones."""
    global llm_global
    modelo = modelo_actual()
    llm_global = OllamaLLM(model=modelo, keep_alive="1h")
    return f"Motor LLM reinstanciado (modelo {modelo}, keep_alive renovado a 1h)"


def _degradar_motor_llm() -> str:
    """Degradacion elegante: cambia al modelo liviano si la latencia sigue
    critica. Si el modelo liviano no esta descargado, no rompe nada: avisa."""
    global llm_global
    actual = modelo_actual()
    if actual == MODELO_LIVIANO:
        llm_global = OllamaLLM(model=MODELO_LIVIANO, keep_alive="1h")
        return f"Ya operando en modelo liviano {MODELO_LIVIANO}; keep_alive renovado"
    try:
        import ollama
        disponibles = [m.get("model", "") for m in ollama.list().get("models", [])]
        if not any(MODELO_LIVIANO in d for d in disponibles):
            return (f"No se pudo degradar: {MODELO_LIVIANO} no esta descargado "
                    f"(ejecuta 'ollama pull {MODELO_LIVIANO}'). Se mantiene {actual}.")
    except Exception:
        pass  # si no podemos verificar, intentamos igual
    llm_global = OllamaLLM(model=MODELO_LIVIANO, keep_alive="1h")
    return f"Degradacion elegante: motor cambiado de {actual} a {MODELO_LIVIANO} para reducir latencia"


def _contingencia_latencia_llm(valor, umbral) -> str:
    """Escalada tipo backoff: el 1er disparo reinstancia el motor; si la
    latencia persiste (disparos siguientes), degrada al modelo liviano."""
    _escalada_llm["n"] += 1
    if _escalada_llm["n"] <= 1:
        return _reiniciar_motor_llm()
    return _degradar_motor_llm()


def _contingencia_latencia_http(valor, umbral) -> str:
    """La latencia HTTP la domina la inferencia del LLM: se reinstancia el
    motor para intentar recuperar tiempos de respuesta."""
    return _reiniciar_motor_llm()


def _contingencia_5xx(valor, umbral) -> str:
    """Ante errores no funcionales (5xx) sostenidos, se reinicia el motor LLM
    (causa habitual: Ollama colgado/timeouts) para intentar recuperarse."""
    return _reiniciar_motor_llm()


monitor.registrar_accion_contingencia(
    "latencia_llm_ms",
    "Reiniciar motor LLM y, si persiste, degradar a modelo liviano",
    _contingencia_latencia_llm,
)
monitor.registrar_accion_contingencia(
    "latencia_http_ms",
    "Reiniciar el motor LLM (domina la latencia HTTP)",
    _contingencia_latencia_http,
)
monitor.registrar_accion_contingencia(
    "tasa_5xx_pct",
    "Reiniciar el motor LLM para recuperar de errores 5xx",
    _contingencia_5xx,
)

def invocar_llm(llm, prompt: str) -> str:
    """Invoca al LLM midiendo su latencia. Toda llamada al modelo pasa por
    aqui, de modo que el monitor conoce el tiempo real de inferencia (que
    es el cuello de botella dominante del sistema, no la base de datos)."""
    import time as _t
    inicio = _t.perf_counter()
    try:
        salida = llm.invoke(prompt)
        monitor.registrar_latencia_llm((_t.perf_counter() - inicio) * 1000, ok=True)
        return salida
    except Exception as e:
        monitor.registrar_latencia_llm((_t.perf_counter() - inicio) * 1000, ok=False)
        raise


historial_conversacion = []
MAX_HISTORIAL_GUARDADO = 30
MAX_HISTORIAL_PARA_LLM = 5

# Memoria de conversacion de corto plazo: se mantiene la lista
# historial_conversacion (la misma que usan los paneles web) y de ahi se
# arma el contexto para el LLM. Antes se usaba ConversationBufferMemory de
# LangChain, retirada por estar deprecada; el buffer propio hace lo mismo
# sin dependencias extra ni warnings.
def _historial_para_llm(n: int = MAX_HISTORIAL_PARA_LLM) -> str:
    return "\n".join(
        f"Cliente: {t['input']}\nMesero: {t['output']}" for t in historial_conversacion[-n:]
    )


def _guardar_turno(pregunta: str, respuesta: str):
    historial_conversacion.append({"input": pregunta, "output": respuesta})
    if len(historial_conversacion) > MAX_HISTORIAL_GUARDADO:
        historial_conversacion.pop(0)


pedido_actual_id = None


# ==================================================================
# LOGICA DEL CHAT (Mesero)
# ==================================================================
async def procesar_mensaje(pregunta: str, llm, usuario_id: int | None = None) -> str:
    global historial_conversacion, pedido_actual_id

    # Analisis de estres: se registra SIEMPRE, en paralelo, sin afectar
    # el flujo normal de la conversacion (es un dato para el monitor,
    # no algo que el usuario vea directamente en el chat).
    try:
        analisis = analizar_mensaje(pregunta)
        # Se atribuye la lectura de carga mental al trabajador que opera el
        # panel (usuario_id de la sesion). Asi el admin ve la carga por persona.
        await db.registrar_interaccion_estres(
            pregunta, analisis["puntaje"], analisis["nivel"], analisis["palabras_detectadas"],
            usuario_id=usuario_id,
        )
        # SENAL DE URGENCIA: si el estres es ALTO y hay un pedido en curso, el
        # Mesero avisa al canal de Cocina para que apure ese pedido.
        if analisis["nivel"] == "alto" and pedido_actual_id is not None:
            try:
                async def _urgente():
                    behav = agent_runtime.agentes["mesero"].MarcarUrgenteBehav(pedido_actual_id, pregunta)
                    agent_runtime.agentes["mesero"].add_behaviour(behav)
                    await behav.join()
                await agent_runtime.ejecutar(_urgente())
                monitor.registrar_evento("warning", f"Estres alto: se pidio apurar el pedido #{pedido_actual_id}", origen="mesero")
            except Exception as e:
                print(f"[Mesero] Aviso: no se pudo enviar la senal de urgencia: {e}")
    except Exception as e:
        print(f"[Mesero] Aviso: no se pudo registrar el analisis de estres: {e}")

    intencion = detectar_intencion(pregunta)
    print(f"[Mesero] Intencion detectada: {intencion}")

    # RESPUESTAS INSTANTANEAS (sin LLM): las intenciones que no necesitan
    # generacion de texto se responden directo. Esto reduce la latencia de
    # ~7s (una llamada a Llama 3) a ~0ms para estos casos.
    import random
    SALUDOS_RAPIDOS = [
        "¡Hola! Bienvenido a DonOso. ¿Que te gustaria pedir? Escribe 'carta' para ver nuestro menu.",
        "¡Buenas! Soy tu asistente de DonOso. ¿En que te puedo ayudar?",
        "¡Hola! ¿Listo para ordenar? Puedo mostrarte la carta si escribes 'carta'.",
    ]

    if intencion == "saludo":
        respuesta_ia = random.choice(SALUDOS_RAPIDOS)
        _guardar_turno(pregunta, respuesta_ia)
        return respuesta_ia

    # CIERRE (respuesta instantanea, sin LLM): el cliente confirma que no
    # quiere agregar nada mas. Antes esto caia en agregar_pedido y disparaba
    # una llamada innecesaria al LLM ademas de una respuesta confusa.
    if intencion == "cierre":
        if pedido_actual_id is not None:
            respuesta_ia = (
                f"¡Perfecto! Tu pedido #{pedido_actual_id} queda asi. "
                "Ya lo tiene cocina. ¿Te ayudo con algo mas?"
            )
        else:
            respuesta_ia = "¡De acuerdo! Si luego quieres ordenar algo, escribe 'carta' para ver el menu."
        _guardar_turno(pregunta, respuesta_ia)
        return respuesta_ia

    if intencion == "ver_carta":
        # La carta sale directo de la BD (precios reales, sin riesgo de
        # que el LLM invente platos o montos).
        carta = await db.obtener_carta()
        lineas = []
        categoria_actual = None
        for plato in carta:
            if plato["categoria"] != categoria_actual:
                categoria_actual = plato["categoria"]
                lineas.append(f"\n{categoria_actual.upper()}")
            lineas.append(f"  • {plato['nombre']} — S/ {plato['precio']:.2f}")
        respuesta_ia = "Esta es nuestra carta:\n" + "\n".join(lineas) + "\n\n¿Que te gustaria pedir?"
        _guardar_turno(pregunta, respuesta_ia)
        return respuesta_ia

    if intencion == "estado_pedido":
        if pedido_actual_id is None:
            # Tal vez hay pedidos activos pero de antes de reiniciar el chat
            activos = await db.pedidos_activos_del_turno()
            if activos:
                lineas = []
                for p in activos[:5]:
                    items_txt = ", ".join(f"{i['cantidad']}x {i['nombre']}" for i in p["items"])
                    lineas.append(f"  #{p['id']}: {items_txt} — {p['estado']}")
                respuesta_ia = f"Hay {len(activos)} pedido(s) activo(s) en este momento:\n" + "\n".join(lineas)
            else:
                respuesta_ia = "No hay ningun pedido activo en este momento."
        else:
            pedido = await db.obtener_pedido(pedido_actual_id)
            estados_legibles = {
                "pendiente": "esta en cola, esperando que cocina lo empiece a preparar",
                "en_preparacion": "se esta preparando en este momento",
                "listo": "ya esta listo y esperando que caja lo cobre",
                "pagado": "ya fue entregado y pagado",
                "cancelado": "fue cancelado",
                "anulado": "fue anulado (devolucion procesada)",
            }
            descripcion = estados_legibles.get(pedido["estado"], pedido["estado"])
            resumen = ", ".join(f"{it['cantidad']}x {it['nombre']}" for it in pedido["items"])
            respuesta_ia = f"Tu pedido #{pedido_actual_id} ({resumen}) {descripcion}."
        _guardar_turno(pregunta, respuesta_ia)
        return respuesta_ia

    if intencion == "listar_pedidos":
        activos = await db.pedidos_activos_del_turno()
        if not activos:
            respuesta_ia = "No hay pedidos activos en este momento. Todos fueron entregados o cancelados."
        else:
            lineas = []
            for p in activos[:8]:
                items_txt = ", ".join(f"{i['cantidad']}x {i['nombre']}" for i in p["items"])
                lineas.append(f"  #{p['id']}: {items_txt} — {p['estado']} (S/ {p['total']:.2f})")
            respuesta_ia = f"Pedidos activos ({len(activos)}):\n" + "\n".join(lineas)
        _guardar_turno(pregunta, respuesta_ia)
        return respuesta_ia

    if intencion == "anular_cobrado":
        if pedido_actual_id is None:
            respuesta_ia = "No hay un pedido reciente para anular. Indica el numero de pedido, por ejemplo: 'anula el pedido 5'."
        else:
            anulado = await db.anular_pedido_cobrado(pedido_actual_id, "solicitado desde el chat")
            if anulado:
                respuesta_ia = (
                    f"Pedido #{pedido_actual_id} anulado. Se procesara como devolucion. "
                    "Avisa al cajero para que registre el reembolso."
                )
                pedido_actual_id = None
            else:
                respuesta_ia = (
                    f"No se pudo anular el pedido #{pedido_actual_id}: "
                    "solo se pueden anular pedidos que ya fueron cobrados (estado 'pagado')."
                )
        _guardar_turno(pregunta, respuesta_ia)
        return respuesta_ia

    if intencion == "cancelar_pedido":
        if pedido_actual_id is None:
            respuesta_ia = "No tienes ningun pedido activo en esta conversacion para cancelar."
        else:
            async def _cancelar():
                behav = agent_runtime.agentes["mesero"].CancelarPedidoBehav(pedido_actual_id)
                agent_runtime.agentes["mesero"].add_behaviour(behav)
                await behav.join()
                return behav.cancelado

            cancelado = await agent_runtime.ejecutar(_cancelar())
            if cancelado:
                respuesta_ia = f"Listo, cancele tu pedido #{pedido_actual_id}."
                pedido_actual_id = None
            else:
                respuesta_ia = (
                    f"No pude cancelar el pedido #{pedido_actual_id}: ya fue pagado o cerrado. "
                    "Si necesitas un reembolso, avisa directamente a un mesero."
                )
        _guardar_turno(pregunta, respuesta_ia)
        return respuesta_ia

    if intencion == "consulta_ingrediente":
        # RAG DEL MENU: recuperamos por similitud los platos relevantes y
        # dejamos que Llama responda SOLO con esos datos reales (ingredientes
        # tomados de la BD via receta_insumo). Asi contesta consultas de
        # alergenos sin inventar.
        try:
            platos_rag = menu_rag.buscar_platos(pregunta, n_resultados=4)
        except Exception as e:
            print(f"[Mesero] Aviso: no se pudo consultar el menu vectorial (Chroma/Ollama): {e}")
            platos_rag = []

        if platos_rag:
            contexto = "\n".join(f"- {p['texto']}" for p in platos_rag)
        else:
            # Respaldo: si el RAG no esta disponible, al menos damos la carta
            # como contexto para no dejar al cliente sin respuesta.
            carta = await db.obtener_carta()
            contexto = "\n".join(f"- {p['nombre']} (S/ {p['precio']:.2f})" for p in carta) \
                or "- (menu no disponible en este momento)"

        prompt_ing = f"""Eres el Agente Mesero de DonOso, una hamburgueseria peruana. El cliente pregunta por ingredientes o alergenos de nuestros platos.

Informacion REAL de nuestros platos (unica fuente valida, no uses nada fuera de esto):
{contexto}

Pregunta del cliente: {pregunta}

Instrucciones:
- Responde corto, claro y en espanol, usando SOLO la informacion de arriba.
- Si preguntan por un alergeno (mani, gluten, lactosa, huevo, sesamo, etc.), razona a partir de los ingredientes listados e indica en que plato(s) aparece o si no aparece.
- Si la informacion no alcanza para estar seguro, dilo con honestidad y sugiere confirmar con el personal. NO inventes ingredientes ni platos.
"""
        try:
            respuesta_ia = invocar_llm(llm, prompt_ing).strip()
        except Exception as e:
            print(f"[Mesero] Error al invocar Ollama: {e}")
            respuesta_ia = "Disculpa, no puedo consultar los ingredientes en este momento. Por favor pregunta al personal."
        _guardar_turno(pregunta, respuesta_ia)
        return respuesta_ia

    if intencion == "consulta_stock":
        bajos = await db.insumos_bajo_stock()
        por_vencer = await db.insumos_por_vencer(dias=5)
        try:
            eventos = chroma_db.buscar_eventos_similares(pregunta, n_resultados=3)
        except Exception as e:
            print(f"[Mesero] Aviso: no se pudo consultar el historial (ChromaDB/Ollama): {e}")
            eventos = []

        contexto_actual = "\n".join(
            f"- {i['nombre']}: {i['stock_actual']} {i['unidad_medida']} (minimo {i['stock_minimo']})" for i in bajos
        ) or "- Ningun insumo esta bajo su stock minimo en este momento."
        contexto_vencimientos = "\n".join(
            f"- {i['nombre']} vence el {i['fecha_vencimiento']}" for i in por_vencer
        ) or "- No hay insumos por vencer en los proximos 5 dias."
        contexto_historial = "\n".join(f"- {e['texto']}" for e in eventos) or "- Sin eventos historicos relevantes."

        prompt_stock = f"""Eres el Agente Mesero de DonOso. Un trabajador te pregunta sobre el inventario.

Datos ACTUALES de stock bajo el minimo:
{contexto_actual}

Insumos por vencer en los proximos 5 dias:
{contexto_vencimientos}

Historial reciente relacionado del almacen:
{contexto_historial}

Pregunta del trabajador: {pregunta}

Responde corto, claro y en espanol, usando SOLO la informacion de arriba. No inventes cifras que no esten en los datos.
"""
        try:
            respuesta_ia = invocar_llm(llm, prompt_stock).strip()
        except Exception as e:
            print(f"[Mesero] Error al invocar Ollama: {e}")
            respuesta_ia = "Disculpa, tengo problemas tecnicos para consultar el inventario en este momento."
        _guardar_turno(pregunta, respuesta_ia)
        return respuesta_ia

    if intencion in ("pedido", "agregar_pedido"):
        # OPTIMIZACION DE LATENCIA: para pedidos usamos UNA SOLA llamada al
        # LLM (la de extraccion de items). La confirmacion se construye con
        # datos reales de la BD, sin texto generado — mas rapido y sin
        # riesgo de alucinacion.
        platos_disponibles = await db.obtener_platos_disponibles()
        items = extraer_items_pedido(pregunta, platos_disponibles, llm)

        if not items:
            respuesta_ia = (
                "No logre identificar ningun plato de nuestra carta en tu mensaje. "
                "¿Podrias ser mas especifico? Escribe 'carta' para ver las opciones."
            )
            _guardar_turno(pregunta, respuesta_ia)
            return respuesta_ia

        intentar_agregar = intencion == "agregar_pedido" and pedido_actual_id is not None
        if intentar_agregar:
            try:
                if not await db.pedido_esta_abierto(pedido_actual_id):
                    raise ValueError("pedido ya cerrado")

                items_antes = len((await db.obtener_pedido(pedido_actual_id))["items"])

                async def _agregar():
                    behav = agent_runtime.agentes["mesero"].AgregarItemsBehav(pedido_actual_id, items)
                    agent_runtime.agentes["mesero"].add_behaviour(behav)
                    await behav.join()

                await agent_runtime.ejecutar(_agregar())

                # VERIFICACION POST-ACCION: se confirma contra la base de datos
                # que los items realmente entraron. No basta con que el agente
                # afirme que lo hizo: los fallos de verificacion representan el
                # 21% de los fallos en sistemas multiagente (MAST, Cemri et al.).
                pedido = await db.obtener_pedido(pedido_actual_id)
                if len(pedido["items"]) <= items_antes:
                    raise RuntimeError("los items no quedaron registrados en la base de datos")

                resumen = ", ".join(f"{it['cantidad']}x {it['nombre']}" for it in pedido["items"])
                respuesta_ia = (
                    f"¡Listo! Agregado a tu pedido #{pedido_actual_id}. Ahora tienes: {resumen}. "
                    f"Nuevo total: S/ {pedido['total']:.2f}."
                )
                _guardar_turno(pregunta, respuesta_ia)
                return respuesta_ia
            except ValueError:
                pass  # el pedido anterior ya cerro: registramos uno nuevo abajo
            except Exception as e:
                print(f"[Mesero] Fallo al agregar items al pedido #{pedido_actual_id}: {e}")
                respuesta_ia = (
                    f"No pude agregar los items a tu pedido #{pedido_actual_id}. "
                    "Por favor avisa a un mesero para que lo haga manualmente."
                )
                _guardar_turno(pregunta, respuesta_ia)
                return respuesta_ia

        async def _tomar_pedido():
            behav = agent_runtime.agentes["mesero"].TomarPedidoBehav(items, usuario_id=usuario_id)
            agent_runtime.agentes["mesero"].add_behaviour(behav)
            await behav.join()
            return behav.pedido_id

        nuevo_pedido_id = await agent_runtime.ejecutar(_tomar_pedido())
        nota_previa = ""
        if intencion == "agregar_pedido":
            if pedido_actual_id is not None:
                nota_previa = "Tu pedido anterior ya estaba cerrado, asi que registre esto como un pedido nuevo. "
            else:
                nota_previa = "No tenias un pedido abierto, asi que registre este como uno nuevo. "
        pedido_actual_id = nuevo_pedido_id

        pedido = await db.obtener_pedido(pedido_actual_id)
        if not pedido["items"]:
            respuesta_ia = "Hubo un problema al registrar tu pedido. Intentalo de nuevo, por favor."
            _guardar_turno(pregunta, respuesta_ia)
            return respuesta_ia

        resumen = ", ".join(f"{it['cantidad']}x {it['nombre']}" for it in pedido["items"])
        respuesta_ia = (
            f"{nota_previa}¡Pedido #{pedido['id']} registrado! ({resumen}). "
            f"Total: S/ {pedido['total']:.2f}. Ya avise a cocina."
        )
        _guardar_turno(pregunta, respuesta_ia)
        return respuesta_ia

    # Charla libre (saludos, preguntas generales): aqui si usamos el LLM
    # conversacional con la memoria de LangChain.
    historial_texto = _historial_para_llm()
    prompt_charla = f"""Eres el Agente Mesero de DonOso, una hamburgueseria peruana.

Historial reciente de la conversacion:
{historial_texto}

Mensaje actual del cliente: {pregunta}

Instrucciones:
- Responde corto, amable y en espanol.
- Si el cliente pregunta por platos o precios, sugierele escribir 'carta' para ver el menu completo. No inventes precios ni platos.
"""
    try:
        respuesta_ia = invocar_llm(llm, prompt_charla).strip()
    except Exception as e:
        print(f"[Mesero] Error al invocar Ollama: {e}")
        respuesta_ia = "Disculpa, tengo problemas tecnicos para responder en este momento."

    _guardar_turno(pregunta, respuesta_ia)
    return respuesta_ia


# (El arranque y el cierre ahora viven en `lifespan`, definido arriba, en
#  lugar de los @app.on_event que estaban deprecados.)


# ==================================================================
# AUTENTICACION (sesiones por cookie) + CONTROL DE ACCESO POR ROL
# ==================================================================
def _usuario_actual(request: Request):
    return auth.obtener_sesion(request.cookies.get("sesion"))


def _contexto(usuario: dict) -> dict:
    """Contexto comun para las plantillas: usuario y paneles que puede ver."""
    return {"usuario": usuario, "paneles": auth.paneles_de(usuario["rol"])}


def _guardia(request: Request, panel: str):
    """Devuelve (usuario, None) si tiene acceso, o (None, RedirectResponse)
    si no hay sesion (-> /login) o el rol no puede ver ese panel (-> su panel)."""
    usuario = _usuario_actual(request)
    if not usuario:
        return None, RedirectResponse("/login", status_code=303)
    if not auth.puede_ver(usuario["rol"], panel):
        return None, RedirectResponse(auth.landing(usuario["rol"]), status_code=303)
    return usuario, None


def _pagina(request: Request, plantilla: str, panel: str):
    usuario, redir = _guardia(request, panel)
    if redir:
        return redir
    return templates.TemplateResponse(request=request, name=plantilla, context=_contexto(usuario))


# ==================================================================
# PANELES (HTML) — protegidos por rol
# ==================================================================
@app.get("/", response_class=HTMLResponse)
async def panel_mesero(request: Request):
    return _pagina(request, "index.html", "mesero")


@app.get("/cocina", response_class=HTMLResponse)
async def panel_cocina(request: Request):
    return _pagina(request, "cocina.html", "cocina")


@app.get("/almacen", response_class=HTMLResponse)
async def panel_almacen(request: Request):
    return _pagina(request, "almacen.html", "almacen")


@app.get("/caja", response_class=HTMLResponse)
async def panel_caja(request: Request):
    return _pagina(request, "caja.html", "caja")


@app.get("/sla", response_class=HTMLResponse)
async def panel_sla(request: Request):
    return _pagina(request, "sla.html", "sla")


@app.get("/monitor", response_class=HTMLResponse)
async def panel_monitor(request: Request):
    return _pagina(request, "monitor.html", "monitor")


@app.get("/admin", response_class=HTMLResponse)
async def panel_admin(request: Request):
    return _pagina(request, "admin.html", "admin")


@app.get("/login", response_class=HTMLResponse)
async def pagina_login(request: Request):
    usuario = _usuario_actual(request)
    if usuario:
        return RedirectResponse(auth.landing(usuario["rol"]), status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")


@app.post("/login")
async def login_post(request: Request):
    body = await request.json()
    usuario = await db.autenticar((body.get("username") or "").strip(), body.get("password") or "")
    if not usuario:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Usuario o contrasena incorrectos."})
    token = auth.crear_sesion(usuario)
    resp = JSONResponse({"ok": True, "landing": auth.landing(usuario["rol"]),
                         "nombre": usuario["nombre"], "rol": usuario["rol"]})
    resp.set_cookie("sesion", token, httponly=True, samesite="lax", max_age=60 * 60 * 8)
    monitor.registrar_evento("info", f"Inicio de sesion: {usuario['username']} ({usuario['rol']})", origen="auth")
    return resp


@app.post("/logout")
async def logout_post(request: Request):
    auth.eliminar_sesion(request.cookies.get("sesion"))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sesion")
    return resp


@app.get("/api/session")
async def api_session(request: Request):
    usuario = _usuario_actual(request)
    if not usuario:
        return {"autenticado": False}
    return {"autenticado": True, "usuario": usuario, "paneles": auth.paneles_de(usuario["rol"])}


@app.post("/api/mi-password")
async def api_mi_password(request: Request):
    """Cambiar la propia contrasena (cualquier usuario con sesion)."""
    usuario = _usuario_actual(request)
    if not usuario:
        return JSONResponse(status_code=401, content={"ok": False, "error": "No autenticado."})
    b = await request.json()
    return await db.cambiar_mi_password(usuario["id"], b.get("actual", ""), b.get("nueva", ""))


# ==================================================================
# API: MESERO
# ==================================================================
@app.get("/chat")
async def chat(request: Request, pregunta: str, usuario_id: int | None = None):
    # Atribuye el pedido al trabajador que inicio sesion (para las metricas
    # de carga de trabajo por persona). Si no hay sesion, usa el usuario_id
    # que venga por query (compatibilidad) o None.
    sesion = _usuario_actual(request)
    uid = sesion["id"] if sesion else usuario_id
    respuesta = await procesar_mensaje(pregunta, llm_global, usuario_id=uid)
    return {"respuesta": respuesta}


@app.get("/api/mesero/historial")
async def api_mesero_historial():
    return {"historial": historial_conversacion}


# ==================================================================
# API: MENU / RAG DE INGREDIENTES
# ==================================================================
@app.post("/api/menu/reindexar")
async def api_menu_reindexar():
    """Reindexa el menu en la coleccion vectorial (util tras editar la carta
    o si el RAG quedo vacio porque Ollama no estaba listo al arrancar)."""
    try:
        platos_rag = await db.obtener_menu_para_rag()
        indexados = menu_rag.reindexar_menu(platos_rag)
        monitor.registrar_evento("info", f"Menu reindexado en el RAG: {indexados} platos", origen="menu_rag")
        return {"ok": True, "indexados": indexados}
    except Exception as e:
        monitor.registrar_evento("error", f"Fallo al reindexar el menu: {e}", origen="menu_rag")
        return {"ok": False, "error": str(e)}


@app.get("/api/menu/buscar")
async def api_menu_buscar(q: str):
    """Busqueda semantica de platos (para probar el RAG desde el navegador)."""
    try:
        return {"resultados": menu_rag.buscar_platos(q, n_resultados=4), "indexados": menu_rag.contar()}
    except Exception as e:
        return {"resultados": [], "error": str(e)}


# ==================================================================
# API: COCINA
# ==================================================================
@app.get("/api/cocina/pedidos")
async def api_cocina_pedidos():
    pendientes = await db.listar_pedidos_por_estado(["pendiente"])
    en_preparacion = await db.listar_pedidos_por_estado(["en_preparacion"])
    return {"pendientes": pendientes, "en_preparacion": en_preparacion,
            "urgentes": sorted(agentes.PEDIDOS_URGENTES)}


@app.post("/api/cocina/iniciar/{pedido_id}")
async def api_cocina_iniciar(pedido_id: int):
    async def _accion():
        behav = agent_runtime.agentes["cocinero"].IniciarPreparacionBehav(pedido_id)
        agent_runtime.agentes["cocinero"].add_behaviour(behav)
        await behav.join()

    await agent_runtime.ejecutar(_accion())
    return {"ok": True}


@app.post("/api/cocina/completar/{pedido_id}")
async def api_cocina_completar(pedido_id: int):
    async def _accion():
        behav = agent_runtime.agentes["cocinero"].CompletarPedidoBehav(pedido_id)
        agent_runtime.agentes["cocinero"].add_behaviour(behav)
        await behav.join()

    await agent_runtime.ejecutar(_accion())
    return {"ok": True}


@app.post("/api/cocina/cancelar/{pedido_id}")
async def api_cocina_cancelar(pedido_id: int):
    async def _accion():
        behav = agent_runtime.agentes["cocinero"].CancelarPedidoBehav(pedido_id)
        agent_runtime.agentes["cocinero"].add_behaviour(behav)
        await behav.join()
        return behav.cancelado

    cancelado = await agent_runtime.ejecutar(_accion())
    return {"ok": cancelado}


# ==================================================================
# API: ALMACEN
# ==================================================================
@app.get("/api/almacen/estado")
async def api_almacen_estado():
    bajos = await db.insumos_bajo_stock()
    por_vencer = await db.insumos_por_vencer(dias=5)
    ag = agent_runtime.agentes["almacenero"]
    avisos = ag.avisos_pendientes if ag else 0
    return {"bajos": bajos, "por_vencer": por_vencer, "avisos_pendientes": avisos}


@app.post("/api/almacen/revisar")
async def api_almacen_revisar():
    async def _accion():
        behav = agent_runtime.agentes["almacenero"].RevisarStockBehav()
        agent_runtime.agentes["almacenero"].add_behaviour(behav)
        await behav.join()
        return behav.resultado

    resultado = await agent_runtime.ejecutar(_accion())
    return resultado


@app.get("/api/almacen/historial")
async def api_almacen_historial(q: str = "novedades del almacen"):
    try:
        eventos = chroma_db.buscar_eventos_similares(q, n_resultados=5)
    except Exception as e:
        return {"eventos": [], "error": str(e)}
    return {"eventos": eventos}


@app.get("/api/almacen/inventario")
async def api_almacen_inventario():
    """Inventario COMPLETO: todos los insumos con su cantidad actual, minimo,
    unidad y vencimiento. Es lo que faltaba mostrar en el panel."""
    return {"insumos": await db.obtener_todos_insumos()}


@app.post("/api/almacen/stock")
async def api_almacen_stock(request: Request):
    """Actualiza el stock de un insumo (edicion manual desde el panel)."""
    body = await request.json()
    nombre = body.get("nombre")
    try:
        nuevo = float(body.get("stock_actual"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "Cantidad invalida"}
    try:
        await db.actualizar_stock_insumo(nombre, nuevo)
        monitor.registrar_evento("info", f"Stock actualizado: {nombre} -> {nuevo}", origen="almacen")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ==================================================================
# API: CAJA
# ==================================================================
@app.get("/api/caja/pedidos")
async def api_caja_pedidos():
    listos = await db.listar_pedidos_por_estado(["listo"])
    return {"listos": listos}


@app.post("/api/caja/cobrar/{pedido_id}")
async def api_caja_cobrar(pedido_id: int):
    async def _accion():
        behav = agent_runtime.agentes["cajero"].CobrarPedidoBehav(pedido_id)
        agent_runtime.agentes["cajero"].add_behaviour(behav)
        await behav.join()
        return behav.total

    total = await agent_runtime.ejecutar(_accion())
    return {"ok": True, "total": total}


@app.post("/api/caja/anular/{pedido_id}")
async def api_caja_anular(pedido_id: int, motivo: str = "anulado desde panel de caja"):
    """Anula un pedido YA COBRADO (devolucion / reembolso)."""
    anulado = await db.anular_pedido_cobrado(pedido_id, motivo)
    if anulado:
        monitor.registrar_evento("warning", f"Pedido #{pedido_id} anulado post-cobro: {motivo}", origen="caja")
    return {"ok": anulado}


@app.get("/api/caja/recientes")
async def api_caja_recientes():
    """Pedidos cobrados recientes (para que el cajero pueda anular si hay un problema)."""
    return {"recientes": await db.listar_pedidos_por_estado(["pagado"])}


@app.get("/api/caja/anulados")
async def api_caja_anulados(dias: int = 3):
    """Pedidos anulados post-cobro (con su motivo) de los ultimos N dias."""
    return {"anulados": await db.listar_anulados_recientes(dias=dias)}


# ==================================================================
# API: SLA y ESTRES
# ==================================================================
@app.get("/api/sla/metricas")
async def api_sla_metricas(desde: str | None = None, hasta: str | None = None):
    return await db.metricas_sla(desde=desde, hasta=hasta)


@app.get("/api/sla/estres")
async def api_sla_estres():
    return await db.resumen_estres()


# ==================================================================
# API: SALUD Y OBSERVABILIDAD (tolerancia a fallos)
# ==================================================================
# ==================================================================
# API: MONITOR / OBSERVABILIDAD (estilo APM)
# ==================================================================
@app.get("/api/monitor/resumen")
async def api_monitor_resumen():
    estado = agent_runtime.estado_salud()
    return monitor.resumen(estado_agentes=estado["agentes"])


@app.get("/api/monitor/serie")
async def api_monitor_serie():
    return monitor.serie_temporal()


@app.get("/api/monitor/codigos")
async def api_monitor_codigos():
    return monitor.distribucion_codigos()


@app.get("/api/monitor/rutas")
async def api_monitor_rutas():
    return {"rutas": monitor.top_rutas()}


@app.get("/api/monitor/errores")
async def api_monitor_errores():
    return {"errores": monitor.errores_recientes()}


@app.get("/api/monitor/eventos")
async def api_monitor_eventos():
    return {"eventos": monitor.eventos_recientes()}


@app.get("/api/monitor/trazas")
async def api_monitor_trazas():
    return {"trazas": monitor.trazas_recientes()}


@app.post("/api/monitor/reiniciar")
async def api_monitor_reiniciar():
    monitor.reiniciar_metricas()
    return {"ok": True}


@app.get("/api/monitor/umbrales")
async def api_monitor_umbrales():
    return {"umbrales": monitor.obtener_umbrales(), "alertas": monitor.alertas_activas()}


@app.post("/api/monitor/umbrales")
async def api_monitor_actualizar_umbrales(request: Request):
    body = await request.json()
    monitor.actualizar_umbrales(body)
    return {"ok": True, "umbrales": monitor.obtener_umbrales()}


@app.get("/api/monitor/contingencia")
async def api_monitor_contingencia():
    """Plan de contingencia (acciones registradas), historial de remediaciones
    ejecutadas, y el modelo LLM en uso actualmente."""
    return {
        "plan": monitor.plan_contingencia(),
        "historial": monitor.historial_contingencia(),
        "modelo_actual": modelo_actual(),
        "modelo_principal": MODELO_PRINCIPAL,
        "degradado": modelo_actual() != MODELO_PRINCIPAL,
    }


@app.get("/api/salud")
async def api_salud():
    """Estado de los 4 agentes: vivo/caido, reinicios, ultimo chequeo."""
    return agent_runtime.estado_salud()


@app.get("/metrics")
async def metrics():
    """Metricas en formato Prometheus, para scrapear desde Grafana."""
    from fastapi.responses import PlainTextResponse
    estado = agent_runtime.estado_salud()
    lineas = [
        "# HELP sma_agente_vivo 1 si el agente esta vivo, 0 si esta caido",
        "# TYPE sma_agente_vivo gauge",
    ]
    for nombre, info in estado["agentes"].items():
        lineas.append(f'sma_agente_vivo{{agente="{nombre}"}} {1 if info["vivo"] else 0}')
    lineas += [
        "# HELP sma_agente_reinicios Reinicios automaticos aplicados por el supervisor",
        "# TYPE sma_agente_reinicios counter",
    ]
    for nombre, info in estado["agentes"].items():
        lineas.append(f'sma_agente_reinicios{{agente="{nombre}"}} {info["reinicios"]}')
    return PlainTextResponse("\n".join(lineas) + "\n")


# ==================================================================
# API: USUARIOS y EXPORTACION (para el analisis estadistico de la tesis)
# ==================================================================
@app.get("/api/usuarios")
async def api_usuarios():
    return {"usuarios": await db.listar_usuarios()}


@app.get("/api/export/pedidos")
async def api_export_pedidos():
    from fastapi.responses import Response
    csv = await db.exportar_pedidos_csv()
    return Response(content="\ufeff" + csv, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=pedidos_sla.csv"})


@app.get("/api/export/estres")
async def api_export_estres():
    from fastapi.responses import Response
    csv = await db.exportar_estres_csv()
    return Response(content="\ufeff" + csv, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=carga_mental.csv"})


# ==================================================================
# API: EXPORTACION A EXCEL (.xlsx)
# ==================================================================
def _items_a_texto(items) -> str:
    return ", ".join(f"{i['cantidad']}x {i['nombre']}" for i in (items or []))


def _respuesta_excel(contenido: bytes, nombre: str):
    from fastapi.responses import Response
    return Response(
        content=contenido,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


def _excel_no_disponible():
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=503, content={
        "ok": False,
        "error": "Falta la libreria openpyxl. Instalala en el entorno con: pip install openpyxl",
    })


@app.get("/api/export/anulados.xlsx")
async def api_export_anulados_excel(dias: int = 3):
    """Exporta a Excel los pedidos anulados (con motivo) de los ultimos N dias."""
    if not export_excel.openpyxl_disponible():
        return _excel_no_disponible()
    anulados = await db.listar_anulados_recientes(dias=dias)
    filas = [{
        "id": a["id"],
        "items": _items_a_texto(a.get("items")),
        "total": a["total"],
        "fecha_hora": a["fecha_hora"],
        "fecha_cancelado": a["fecha_cancelado"],
        "motivo_anulacion": a["motivo_anulacion"],
    } for a in anulados]
    columnas = [
        ("id", "Pedido #"), ("items", "Items"), ("total", "Total S/"),
        ("fecha_hora", "Fecha pedido"), ("fecha_cancelado", "Fecha anulacion"),
        ("motivo_anulacion", "Motivo"),
    ]
    contenido = export_excel.construir_libro([("Anulados", columnas, filas)])
    return _respuesta_excel(contenido, "anulados_donoso.xlsx")


@app.get("/api/export/excel")
async def api_export_excel():
    """Reporte consolidado en Excel (una hoja por tema): pedidos/SLA, anulados,
    estres e inventario. Es 'las cosas importantes' en un solo archivo."""
    if not export_excel.openpyxl_disponible():
        return _excel_no_disponible()

    pedidos = await db.filas_pedidos_export()
    anulados = await db.listar_anulados_recientes(dias=3650)  # historico completo
    carga = await db.filas_estres_export()
    carga_dia = await db.carga_mental_diaria()
    carga_usuario = await db.carga_mental_por_usuario()
    insumos = await db.obtener_todos_insumos()

    # Etiqueta legible del nivel (bajo->Baja, medio->Media, alto->Alta).
    NIVEL = db.NIVEL_CARGA
    carga = [{**c, "nivel_txt": NIVEL.get(c.get("nivel"), c.get("nivel"))} for c in carga]

    # Fila TOTAL para el resumen diario (%diario + total del mes completo).
    tot_baja = sum(d["baja"] or 0 for d in carga_dia)
    tot_media = sum(d["media"] or 0 for d in carga_dia)
    tot_alta = sum(d["alta"] or 0 for d in carga_dia)
    tot_total = sum(d["total"] or 0 for d in carga_dia)
    fila_total = {
        "fecha": "TOTAL", "baja": tot_baja, "media": tot_media, "alta": tot_alta,
        "total": tot_total,
        "pct_alta": round(100.0 * tot_alta / tot_total, 1) if tot_total else 0.0,
    }
    carga_dia_filas = carga_dia + [fila_total]

    hojas = [
        ("Pedidos (SLA)", [
            ("id", "Pedido #"), ("trabajador", "Trabajador"), ("estado", "Estado"), ("total", "Total S/"),
            ("fecha_hora", "Llegada"), ("fecha_inicio_preparacion", "Inicio prep."), ("fecha_listo", "Listo"),
            ("fecha_pagado", "Pagado"), ("fecha_cancelado", "Cancelado/Anulado"),
            ("motivo_anulacion", "Motivo anulacion"),
            ("seg_toma", "Seg. toma"), ("seg_preparacion", "Seg. preparacion"),
        ], pedidos),
        ("Anulados", [
            ("id", "Pedido #"), ("items", "Items"), ("total", "Total S/"),
            ("fecha_hora", "Fecha pedido"), ("fecha_cancelado", "Fecha anulacion"),
            ("motivo_anulacion", "Motivo"),
        ], [{**a, "items": _items_a_texto(a.get("items"))} for a in anulados]),
        ("Carga mental", [
            ("id", "#"), ("fecha_hora", "Fecha"), ("trabajador", "Trabajador"),
            ("nivel_txt", "Carga mental"), ("puntaje", "Puntaje"),
            ("palabras_clave", "Senales"), ("mensaje", "Mensaje"),
        ], carga),
        ("Carga mental (diario)", [
            ("fecha", "Fecha"), ("baja", "Baja"), ("media", "Media"), ("alta", "Alta"),
            ("total", "Total"), ("pct_alta", "% Carga alta (diario)"),
        ], carga_dia_filas),
        ("Carga mental x trabajador", [
            ("trabajador", "Trabajador"), ("rol", "Rol"),
            ("baja", "Baja"), ("media", "Media"), ("alta", "Alta"), ("total", "Total"),
            ("pct_alta", "% Carga alta"), ("indice_carga", "Indice carga (0-100)"),
        ], carga_usuario),
        ("Inventario", [
            ("nombre", "Insumo"), ("stock_actual", "Stock actual"), ("stock_minimo", "Stock minimo"),
            ("unidad_medida", "Unidad"), ("fecha_vencimiento", "Vence"),
        ], insumos),
    ]
    contenido = export_excel.construir_libro(hojas)
    return _respuesta_excel(contenido, "reporte_donoso.xlsx")


# ==================================================================
# API: ADMINISTRACION (solo rol admin)
# ==================================================================
def _admin_o_403(request: Request):
    """Devuelve (usuario, None) si es admin, o (None, JSONResponse 403)."""
    usuario = _usuario_actual(request)
    if not usuario or usuario["rol"] != "admin":
        return None, JSONResponse(status_code=403, content={"ok": False, "error": "Requiere rol admin."})
    return usuario, None


async def _reindexar_menu_seguro():
    """Reindexa el RAG del menu tras un cambio en la carta (sin romper si falla)."""
    try:
        platos = await db.obtener_menu_para_rag()
        menu_rag.reindexar_menu(platos)
    except Exception as e:
        monitor.registrar_evento("warning", f"No se pudo reindexar el menu tras el cambio: {e}", origen="menu_rag")


# ---- Comportamiento de los agentes ----
@app.get("/api/agentes/actividad")
async def api_agentes_actividad(request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    salud = agent_runtime.estado_salud()
    return {"agentes": salud["agentes"], "actividad": monitor.actividad_agentes(80)}


# ---- Carga de trabajo mental (por trabajador, por rol y evolucion diaria) ----
def _mejora_desde_diaria(diaria: list) -> dict | None:
    """% de carga alta al inicio (primeros dias) vs al final (ultimos dias)."""
    if len(diaria) < 2:
        return None
    ini = diaria[0]["pct_alta"]     # primer dia (la linea arranca aqui)
    fin = diaria[-1]["pct_alta"]    # ultimo dia
    return {"inicio_pct_alta": round(ini, 1), "final_pct_alta": round(fin, 1),
            "reduccion_pct": round(ini - fin, 1)}


@app.get("/api/admin/carga-mental")
async def api_admin_carga_mental(request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    # Devolvemos las filas crudas: el panel agrega y filtra en el navegador
    # (rol, rango de fechas, orden) sin recargar. 'ultima' ancla los presets
    # relativos ("ultimas 4 horas", "ultimos 2 dias") a la fecha mas reciente.
    filas = await db.carga_mental_interacciones()
    ultima = filas[-1]["f"] if filas else None
    return {
        "roles": db.ROLES_TRABAJADOR,
        "interacciones": filas,
        "ultima": ultima,
    }


@app.get("/api/admin/carga-mental/export")
async def api_admin_carga_mental_export(request: Request, rol: str | None = None,
                                        desde: str | None = None, hasta: str | None = None):
    """Exporta la carga mental a Excel. Con ?rol= exporta solo ese rol y con
    ?desde=&hasta= solo ese rango de fechas (respeta el filtro del panel); sin
    filtros exporta TODO. Incluye % diario, total y % general."""
    usuario, err = _admin_o_403(request)
    if err:
        return err
    if not export_excel.openpyxl_disponible():
        return _excel_no_disponible()

    ambito = rol if (rol and rol != "todos") else None
    general = await db.carga_mental_general(ambito, desde, hasta)
    por_rol = await db.carga_mental_por_rol(desde, hasta)
    diaria = await db.carga_mental_diaria(ambito, desde, hasta)
    por_usuario = await db.carga_mental_por_usuario(ambito, desde, hasta)

    etiqueta = (ambito or "GENERAL (todos los roles)")

    # Hoja RESUMEN: fila general (con su total y % general) + una fila por rol.
    resumen = [{
        "ambito": f"GENERAL — {etiqueta}" if ambito else "GENERAL (todos los roles)",
        "baja": general["baja"], "media": general["media"], "alta": general["alta"],
        "total": general["total"],
        "pct_baja": general["pct_baja"], "pct_media": general["pct_media"], "pct_alta": general["pct_alta"],
    }]
    for pr in por_rol:
        if ambito and pr["rol"] != ambito:
            continue
        resumen.append({
            "ambito": f"Rol: {pr['rol']}",
            "baja": pr["baja"], "media": pr["media"], "alta": pr["alta"], "total": pr["total"],
            "pct_baja": pr["pct_baja"], "pct_media": pr["pct_media"], "pct_alta": pr["pct_alta"],
        })

    # Hoja DIARIO: % diario de cada nivel + fila TOTAL (cuyos % son el % general).
    diaria_filas = [{
        "fecha": d["fecha"], "baja": d["baja"], "media": d["media"], "alta": d["alta"],
        "total": d["total"], "pct_baja": d["pct_baja"], "pct_media": d["pct_media"], "pct_alta": d["pct_alta"],
    } for d in diaria]
    diaria_filas.append({
        "fecha": "TOTAL / % GENERAL", "baja": general["baja"], "media": general["media"],
        "alta": general["alta"], "total": general["total"],
        "pct_baja": general["pct_baja"], "pct_media": general["pct_media"], "pct_alta": general["pct_alta"],
    })

    cols_resumen = [
        ("ambito", "Ambito"), ("baja", "Baja"), ("media", "Media"), ("alta", "Alta"), ("total", "Total"),
        ("pct_baja", "% Baja"), ("pct_media", "% Media"), ("pct_alta", "% Alta"),
    ]
    cols_diario = [
        ("fecha", "Fecha"), ("baja", "Baja"), ("media", "Media"), ("alta", "Alta"), ("total", "Total"),
        ("pct_baja", "% Baja (diario)"), ("pct_media", "% Media (diario)"), ("pct_alta", "% Alta (diario)"),
    ]
    cols_trab = [
        ("trabajador", "Trabajador"), ("rol", "Rol"),
        ("baja", "Baja"), ("media", "Media"), ("alta", "Alta"), ("total", "Total"),
        ("pct_alta", "% Alta"), ("indice_carga", "Indice carga (0-100)"),
    ]
    hojas = [
        ("Resumen (general y rol)", cols_resumen, resumen),
        ("Diario (% diario)", cols_diario, diaria_filas),
        ("Por trabajador", cols_trab, por_usuario),
    ]
    contenido = export_excel.construir_libro(hojas)
    nombre = f"carga_mental_{ambito}.xlsx" if ambito else "carga_mental_general.xlsx"
    return _respuesta_excel(contenido, nombre)


# ---- Gestion de la carta ----
@app.get("/api/admin/menu")
async def api_admin_menu(request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    return {"platos": await db.obtener_menu_admin(), "categorias": await db.listar_categorias()}


@app.post("/api/admin/menu")
async def api_admin_menu_crear(request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    b = await request.json()
    try:
        cat = b.get("categoria_id")
        if not cat and b.get("categoria"):
            cat = await db.obtener_o_crear_categoria(b["categoria"])
        pid = await db.crear_plato(b["nombre"], b.get("descripcion", ""), b["precio"], cat,
                                   1 if b.get("disponible", True) else 0)
        await _reindexar_menu_seguro()
        monitor.registrar_evento("info", f"Plato creado: {b['nombre']}", origen="admin")
        return {"ok": True, "id": pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/admin/menu/{plato_id}")
async def api_admin_menu_actualizar(plato_id: int, request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    b = await request.json()
    try:
        cat = b.get("categoria_id")
        if not cat and b.get("categoria"):
            cat = await db.obtener_o_crear_categoria(b["categoria"])
        await db.actualizar_plato(plato_id, b["nombre"], b.get("descripcion", ""), b["precio"], cat,
                                  1 if b.get("disponible", True) else 0)
        await _reindexar_menu_seguro()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/admin/menu/{plato_id}/eliminar")
async def api_admin_menu_eliminar(plato_id: int, request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    try:
        resultado = await db.eliminar_plato(plato_id)
        await _reindexar_menu_seguro()
        return {"ok": True, "resultado": resultado}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/admin/menu/{plato_id}/receta")
async def api_admin_receta(plato_id: int, request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    return {"receta": await db.obtener_receta(plato_id), "insumos": await db.obtener_insumos_admin()}


@app.post("/api/admin/menu/{plato_id}/receta")
async def api_admin_receta_guardar(plato_id: int, request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    b = await request.json()
    try:
        res = await db.guardar_receta(plato_id, b.get("items", []))
        await _reindexar_menu_seguro()  # la receta alimenta el RAG de alergenos
        monitor.registrar_evento("info", f"Receta actualizada (plato #{plato_id}): {res['items']} insumos", origen="admin")
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/admin/menu/importar")
async def api_admin_menu_importar(request: Request):
    """Importa platos desde Excel (.xlsx), Word (.docx) o PDF (.pdf).
    - Excel: columnas categoria, nombre, descripcion, precio, disponible.
    - Word/PDF: se extrae el texto y el LLM (Ollama) identifica los platos."""
    usuario, err = _admin_o_403(request)
    if err:
        return err
    import base64
    b = await request.json()
    filename = (b.get("filename") or "").lower()
    try:
        datos = base64.b64decode((b.get("data") or "").split(",")[-1])
    except Exception as e:
        return {"ok": False, "error": f"Archivo invalido: {e}"}

    if filename.endswith(".docx"):
        if not import_docs.disponible_docx():
            return {"ok": False, "error": "Falta python-docx (pip install python-docx)."}
        try:
            texto = import_docs.extraer_texto_docx(datos)
        except Exception as e:
            return {"ok": False, "error": f"No se pudo leer el Word: {e}"}
        return await _importar_desde_texto(texto)

    if filename.endswith(".pdf"):
        if not import_docs.disponible_pdf():
            return {"ok": False, "error": "Falta pypdf (pip install pypdf)."}
        try:
            texto = import_docs.extraer_texto_pdf(datos)
        except Exception as e:
            return {"ok": False, "error": f"No se pudo leer el PDF: {e}"}
        return await _importar_desde_texto(texto)

    # Por defecto: Excel
    if not export_excel.openpyxl_disponible():
        return {"ok": False, "error": "Falta openpyxl (pip install openpyxl)."}
    return await _importar_xlsx(datos)


async def _importar_xlsx(datos: bytes) -> dict:
    import io
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(datos), data_only=True)
        ws = wb.active
        filas = list(ws.iter_rows(values_only=True))
    except Exception as e:
        return {"ok": False, "error": f"No se pudo leer el Excel: {e}"}
    if not filas:
        return {"ok": False, "error": "El archivo esta vacio."}
    encabezados = [str(c).strip().lower() if c is not None else "" for c in filas[0]]
    def idx(*nombres):
        for n in nombres:
            if n in encabezados:
                return encabezados.index(n)
        return None
    i_cat, i_nom, i_desc, i_pre, i_disp = (
        idx("categoria", "categoría"), idx("nombre", "plato"),
        idx("descripcion", "descripción"), idx("precio", "precio s/"), idx("disponible"),
    )
    if i_nom is None or i_pre is None:
        return {"ok": False, "error": "El Excel debe tener al menos las columnas 'nombre' y 'precio'."}
    creados = actualizados = errores = 0
    for fila in filas[1:]:
        try:
            nombre = fila[i_nom]
            if nombre is None or str(nombre).strip() == "":
                continue
            precio = float(fila[i_pre])
            categoria = fila[i_cat] if i_cat is not None and fila[i_cat] else "Otros"
            descripcion = fila[i_desc] if i_desc is not None and fila[i_desc] else ""
            disponible = 1
            if i_disp is not None and fila[i_disp] is not None:
                disponible = 0 if str(fila[i_disp]).strip().lower() in ("0", "no", "false", "n") else 1
            res = await db.upsert_plato_por_nombre(str(nombre), str(descripcion), precio, str(categoria), disponible)
            creados += res == "creado"
            actualizados += res == "actualizado"
        except Exception:
            errores += 1
    await _reindexar_menu_seguro()
    monitor.registrar_evento("info", f"Menu importado (Excel): {creados} nuevos, {actualizados} act., {errores} err.", origen="admin")
    return {"ok": True, "creados": creados, "actualizados": actualizados, "errores": errores, "via": "Excel"}


async def _importar_desde_texto(texto: str) -> dict:
    """Word/PDF: el LLM extrae los platos del texto libre."""
    if not texto or not texto.strip():
        return {"ok": False, "error": "No se pudo extraer texto del documento (¿esta escaneado?)."}
    prompt = (
        "Del siguiente texto de una carta de restaurante, extrae los platos.\n"
        "Devuelve UNICAMENTE un arreglo JSON valido, sin texto adicional, con objetos:\n"
        '[{"categoria": "...", "nombre": "...", "descripcion": "...", "precio": 0.0}]\n'
        "El precio debe ser numero (sin 'S/'). Si no hay precio, usa 0. No inventes platos.\n\n"
        f"TEXTO:\n{texto[:6000]}"
    )
    try:
        salida = await asyncio.to_thread(invocar_llm, llm_global, prompt)
    except Exception as e:
        return {"ok": False, "error": f"El LLM (Ollama) no respondio: {e}"}
    import json, re
    s = salida.strip().strip("`")
    m = re.search(r"\[.*\]", s, re.S)
    if m:
        s = m.group(0)
    try:
        items = json.loads(s)
    except Exception:
        return {"ok": False, "error": "El modelo no devolvio un JSON valido. Prueba con un Excel."}
    if not isinstance(items, list):
        return {"ok": False, "error": "Formato inesperado del modelo."}
    creados = actualizados = errores = 0
    for it in items:
        try:
            if not isinstance(it, dict):
                continue
            nombre = str(it.get("nombre", "")).strip()
            if not nombre:
                continue
            precio = float(it.get("precio") or 0)
            res = await db.upsert_plato_por_nombre(nombre, str(it.get("descripcion") or ""),
                                                   precio, str(it.get("categoria") or "Otros"), 1)
            creados += res == "creado"
            actualizados += res == "actualizado"
        except Exception:
            errores += 1
    await _reindexar_menu_seguro()
    monitor.registrar_evento("info", f"Menu importado (Word/PDF via LLM): {creados} nuevos, {actualizados} act., {errores} err.", origen="admin")
    return {"ok": True, "creados": creados, "actualizados": actualizados, "errores": errores, "via": "Word/PDF (LLM)"}


@app.get("/api/admin/menu/plantilla.xlsx")
async def api_admin_menu_plantilla(request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    if not export_excel.openpyxl_disponible():
        return _excel_no_disponible()
    columnas = [("categoria", "categoria"), ("nombre", "nombre"), ("descripcion", "descripcion"),
                ("precio", "precio"), ("disponible", "disponible")]
    ejemplo = [
        {"categoria": "Hamburguesas", "nombre": "Ejemplo Clasica", "descripcion": "Pan, carne, queso",
         "precio": 18.0, "disponible": 1},
        {"categoria": "Bebidas", "nombre": "Ejemplo Gaseosa", "descripcion": "500ml", "precio": 6.0, "disponible": 1},
    ]
    contenido = export_excel.construir_libro([("Menu", columnas, ejemplo)])
    return _respuesta_excel(contenido, "plantilla_menu_donoso.xlsx")


# ---- Gestion de usuarios (delegar permisos) ----
@app.get("/api/admin/usuarios")
async def api_admin_usuarios(request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    return {"usuarios": await db.listar_usuarios_admin(), "roles": list(db.ROLES_VALIDOS)}


@app.post("/api/admin/usuarios")
async def api_admin_usuarios_crear(request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    b = await request.json()
    res = await db.crear_usuario(b.get("nombre", ""), b.get("username", ""), b.get("password", ""), b.get("rol", ""))
    if res.get("ok"):
        monitor.registrar_evento("info", f"Usuario creado: {b.get('username')} ({b.get('rol')})", origen="admin")
    return res


@app.post("/api/admin/usuarios/{usuario_id}")
async def api_admin_usuarios_actualizar(usuario_id: int, request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    b = await request.json()
    return await db.actualizar_usuario(usuario_id, nombre=b.get("nombre"), rol=b.get("rol"), activo=b.get("activo"))


@app.post("/api/admin/usuarios/{usuario_id}/password")
async def api_admin_usuarios_password(usuario_id: int, request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    b = await request.json()
    return await db.cambiar_password(usuario_id, b.get("password", ""))


# ---- Gestion de ingredientes / insumos ----
@app.get("/api/admin/insumos")
async def api_admin_insumos(request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    return {"insumos": await db.obtener_insumos_admin()}


@app.post("/api/admin/insumos")
async def api_admin_insumos_crear(request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    b = await request.json()
    res = await db.crear_insumo(b.get("nombre"), b.get("unidad_medida"), b.get("stock_actual"),
                                b.get("stock_minimo"), b.get("fecha_vencimiento"), b.get("proveedor"))
    if res.get("ok"):
        monitor.registrar_evento("info", f"Insumo creado: {b.get('nombre')}", origen="admin")
    return res


@app.post("/api/admin/insumos/{insumo_id}")
async def api_admin_insumos_actualizar(insumo_id: int, request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    b = await request.json()
    return await db.actualizar_insumo(insumo_id, b.get("nombre"), b.get("unidad_medida"),
                                      b.get("stock_actual"), b.get("stock_minimo"),
                                      b.get("fecha_vencimiento"), b.get("proveedor"))


@app.post("/api/admin/insumos/{insumo_id}/eliminar")
async def api_admin_insumos_eliminar(insumo_id: int, request: Request):
    usuario, err = _admin_o_403(request)
    if err:
        return err
    return await db.eliminar_insumo(insumo_id)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)