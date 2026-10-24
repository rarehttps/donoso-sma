"""
nlp_pedido.py
--------------
Capa de NLP del sistema:
  1. spaCy detecta la INTENCION del mensaje (pedido / consulta_stock / saludo / otro)
  2. Si la intencion es "pedido", Ollama (Llama 3) EXTRAE los items, pero
     SOLO puede usar nombres que existan en la carta real (se le pasa el
     menu como contexto). Esto evita que el LLM invente platos que no
     existen ("grounding" simple, sin necesidad de ChromaDB todavia
     porque la carta es corta y cabe entera en el prompt).
"""
import json
import re
import unicodedata
import spacy

try:
    _nlp = spacy.load("es_core_news_sm")
except OSError:
    print("[nlp_pedido] AVISO: modelo es_core_news_sm no encontrado, usando pipeline basico.")
    _nlp = spacy.blank("es")

# ---------------------------------------------------------------
# DETECCION DE INTENCION
# ---------------------------------------------------------------
# Se comparan RAICES (prefijos), no palabras completas, y sobre texto
# SIN ACENTOS. Motivo: spaCy lematiza mal los encliticos del espanol
# ("agregame" -> "agregamir", no "agregar") y los acentos rompen la
# comparacion exacta ("tambien" != "tambien" con tilde). Sin esto,
# "agregame tequenos a mi pedido" se clasificaba como PEDIDO NUEVO
# porque contenia la palabra "pedido" — el modo de fallo por
# mala interpretacion de la tarea (MAST, Cemri et al., NeurIPS 2025).

RAICES_CANCELAR = ("cancel", "anul", "olvid", "quit", "elimin")
RAICES_AGREGAR  = ("agreg", "anad", "sumal", "sumam", "incluy", "tambien", "ademas", "aparte", "adicional", "otro mas", "ponme")
RAICES_ANULAR   = ("devol", "reembols", "anula")  # post-cobro, distinto de cancelar
RAICES_BUSCAR   = ("busca", "cual", "donde", "cuantos", "tienen")
RAICES_LISTAR   = ("mis pedidos", "pedidos activos", "que tengo", "mis ordenes", "que pedidos")
RAICES_PEDIDO   = ("quier", "pedir", "pedid", "pido", "orden", "comanda", "dame", "traeme", "trae", "me da", "voy a pedir", "necesit")
RAICES_STOCK    = ("stock", "inventario", "qued", "disponib", "insumo")
RAICES_SALUDO   = ("hola", "buenas", "buenos dias", "saludos", "que tal")

FRASES_ESTADO = ("como va", "como esta", "ya esta", "esta listo", "cuanto falta",
                 "que tal va", "como vamos", "ya salio", "falta mucho", "en que va")
FRASES_CARTA  = ("carta", "menu", "precio", "que venden", "que tienen", "que hay",
                 "opciones", "que ofrecen")
PALABRAS_ORDEN = ("pedido", "orden", "comanda")

# CONSULTA DE INGREDIENTES / ALERGENOS (RAG del menu). Se evalua DESPUES de
# "pedido" para no robarse pedidos reales ("quiero una hamburguesa sin mani"
# sigue siendo un pedido), pero captura preguntas como "el superoso tiene
# mani?", "algo sin gluten", "que lleva la donoso".
PALABRAS_ALERGENO = ("alergi", "alergen", "gluten", "mani", "cacahuate", "lactosa",
                     "lacteo", "celiac", "intoleran", "sesamo", "ajonjoli", "vegan",
                     "vegetarian", "picante", "gaseosa", "azucar")
FRASES_INGREDIENTE = ("que lleva", "que trae", "que contiene", "contiene", "ingrediente",
                      "de que esta hecho", "de que esta hecha", "esta hecho con",
                      "lleva", "sin gluten", "sin lactosa", "sin tacc", "apto para")

# FRASES DE CIERRE / NEGACION: cuando el cliente confirma que NO quiere
# agregar nada mas. Se evaluan ANTES que RAICES_AGREGAR porque frases como
# "no quiero agregar nada mas" contienen la raiz "agreg" y se clasificaban
# erroneamente como agregar_pedido (disparando una llamada innecesaria al
# LLM). Son frases especificas (con negacion explicita) para no bloquear
# pedidos reales que casualmente terminen en "nada mas".
FRASES_CIERRE = (
    "no quiero agregar", "no quiero nada", "no deseo agregar", "no deseo nada",
    "no agregues", "no agregar", "no gracias", "nada mas gracias",
    "asi esta bien", "asi estamos bien", "estamos bien asi", "eso es todo",
    "eso seria todo", "eso nomas", "ya no quiero", "ya no gracias", "ya es todo",
)


def _sin_acentos(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _contiene_raiz(texto_norm: str, raices) -> bool:
    """True si alguna raiz aparece como inicio de palabra en el texto."""
    palabras = re.findall(r"[a-z0-9]+", texto_norm)
    for raiz in raices:
        if " " in raiz:
            if raiz in texto_norm:
                return True
        elif any(pal.startswith(raiz) for pal in palabras):
            return True
    return False


def detectar_intencion(texto: str) -> str:
    t = _sin_acentos(texto)

    # 0) CIERRE / NEGACION: "no quiero agregar nada mas", "asi esta bien".
    # Debe ir PRIMERO para no caer en agregar_pedido (que dispararia el LLM).
    if any(f in t for f in FRASES_CIERRE):
        return "cierre"

    # ORDEN CRITICO: las intenciones especificas se evaluan ANTES que
    # "pedido", porque todas pueden contener la palabra "pedido".
    # 1) Consultar estado: "como va mi pedido"
    if any(f in t for f in FRASES_ESTADO) and any(w in t for w in PALABRAS_ORDEN):
        return "estado_pedido"
    # 2) Listar pedidos activos: "mis pedidos", "que tengo"
    if any(f in t for f in RAICES_LISTAR):
        return "listar_pedidos"
    # 3) Devolucion/reembolso post-cobro: "quiero devolucion"
    if _contiene_raiz(t, RAICES_ANULAR) and any(w in t for w in PALABRAS_ORDEN):
        return "anular_cobrado"
    # 4) Cancelar (pre-cobro): "cancela el pedido"
    if _contiene_raiz(t, RAICES_CANCELAR):
        return "cancelar_pedido"
    # 5) Agregar a un pedido existente: "agregame tequenos"
    if _contiene_raiz(t, RAICES_AGREGAR):
        return "agregar_pedido"
    # 4) Ver la carta
    if any(f in t for f in FRASES_CARTA):
        return "ver_carta"
    # 5) Pedido nuevo
    if _contiene_raiz(t, RAICES_PEDIDO):
        return "pedido"
    # 6) Consulta de ingredientes / alergenos (RAG del menu)
    if _contiene_raiz(t, PALABRAS_ALERGENO) or any(f in t for f in FRASES_INGREDIENTE):
        return "consulta_ingrediente"
    if _contiene_raiz(t, RAICES_STOCK):
        return "consulta_stock"
    if _contiene_raiz(t, RAICES_SALUDO):
        return "saludo"
    return "otro"


def _normalizar(texto: str) -> str:
    texto = _sin_acentos(texto).strip()
    texto = texto.replace("-", " ")
    texto = re.sub(r"[^a-z0-9 ]", "", texto)
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def _buscar_plato_real(nombre_extraido: str, platos_disponibles: list[str]) -> str | None:
    """
    Compara de forma flexible: 'Coca-Cola' debe encontrar 'Coca-Cola 600ml'.
    1) match exacto (normalizado), 2) uno contiene al otro como substring.
    """
    objetivo = _normalizar(nombre_extraido)
    if not objetivo:
        return None

    normalizados = {p: _normalizar(p) for p in platos_disponibles}

    for original, norm in normalizados.items():
        if norm == objetivo:
            return original

    for original, norm in normalizados.items():
        if objetivo in norm or norm in objetivo:
            return original

    return None


def _registrar_latencia(ms: float, ok: bool):
    """Reporta la latencia al monitor si esta disponible (import perezoso
    para no crear una dependencia circular entre modulos)."""
    try:
        import monitor
        monitor.registrar_latencia_llm(ms, ok=ok)
    except Exception:
        pass


def _construir_prompt_extraccion(texto: str, platos_disponibles: list[str]) -> str:
    menu = "\n".join(f"- {p}" for p in platos_disponibles)
    return f"""Eres un sistema de extraccion de pedidos para un restaurante. Tu UNICA tarea es identificar que platos del MENU OFICIAL pidio el cliente y en que cantidad.

MENU OFICIAL (son los UNICOS nombres validos; copia el nombre EXACTO tal como aparece):
{menu}

Texto del cliente: "{texto}"

Responde UNICAMENTE con un arreglo JSON valido, sin texto adicional, sin explicaciones, con este formato exacto:
[{{"plato": "Nombre exacto del menu", "cantidad": 1}}]

Si el cliente no pidio ningun plato que exista en el menu, responde exactamente: []"""


def extraer_items_pedido(texto: str, platos_disponibles: list[str], llm) -> list[dict]:
    """
    llm: cualquier objeto con metodo .invoke(prompt) -> str (ej: OllamaLLM)
    Devuelve una lista de {"plato": <nombre real>, "cantidad": <int>} SOLO
    con platos que de verdad existen en platos_disponibles.
    """
    prompt = _construir_prompt_extraccion(texto, platos_disponibles)

    import time as _t
    _ini = _t.perf_counter()
    try:
        respuesta = llm.invoke(prompt).strip()
        _registrar_latencia((_t.perf_counter() - _ini) * 1000, True)
    except Exception as e:
        _registrar_latencia((_t.perf_counter() - _ini) * 1000, False)
        print(f"[nlp_pedido] Error al invocar el LLM: {e}")
        return []

    respuesta = respuesta.strip("`")
    if respuesta.lower().startswith("json"):
        respuesta = respuesta[4:].strip()

    try:
        items_crudos = json.loads(respuesta)
    except json.JSONDecodeError:
        print(f"[nlp_pedido] El LLM no devolvio JSON valido: {respuesta!r}")
        return []

    if not isinstance(items_crudos, list):
        return []

    nombres_validos = {p.lower(): p for p in platos_disponibles}
    items_validados = []
    for item in items_crudos:
        if not isinstance(item, dict):
            continue
        nombre_extraido = str(item.get("plato", "")).strip()
        cantidad = item.get("cantidad", 1)

        nombre_real = _buscar_plato_real(nombre_extraido, platos_disponibles)

        if nombre_real and isinstance(cantidad, (int, float)) and cantidad > 0:
            items_validados.append({"plato": nombre_real, "cantidad": int(cantidad)})
        elif nombre_extraido:
            print(f"[nlp_pedido] Descartado (no existe en el menu real): '{nombre_extraido}'")

    return items_validados