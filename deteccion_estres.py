"""
deteccion_estres.py
---------------------
Analiza cada mensaje del chat en busca de senales textuales de estres
o urgencia (palabras clave, signos de exclamacion, mayusculas excesivas).
Pensado para horarios de alta demanda (rush), como un indicador
complementario a CarMen-Q: mientras CarMen-Q mide la carga percibida
de los TRABAJADORES via cuestionario, esto da una senal en tiempo real
de la presion que transmiten los CLIENTES en el chat, util para
correlacionar picos de estres textual con los SLA de tiempo de espera.

No usa un modelo de IA: es deliberadamente simple y explicable (cada
punto del puntaje tiene una causa clara), para que sea facil de
justificar metodologicamente en la tesis.
"""
import re

PALABRAS_ALTA_URGENCIA = {
    "urgente", "rapido", "ya", "apurate", "apurense", "apuren",
    "demora", "demoran", "tardanza", "tarde", "esperando", "espera",
    "molesto", "molesta", "harto", "harta", "pesimo", "pesima",
    "terrible", "horrible", "inaceptable", "increible",
}

PALABRAS_MEDIA_URGENCIA = {
    "cuanto", "falta", "todavia", "aun", "porfavor", "favor", "necesito",
    "rapidito", "pronto",
}


def analizar_mensaje(texto: str) -> dict:
    """
    Devuelve {puntaje, nivel, palabras_detectadas}.
    Puntaje: suma de senales encontradas (cada una vale un punto, salvo
    mayusculas/exclamaciones que valen lo que se indica abajo).
    Nivel: 'bajo' (0-1), 'medio' (2-3), 'alto' (4+).
    """
    texto_low = texto.lower()
    palabras = re.findall(r"[a-záéíóúñ]+", texto_low)
    palabras_set = set(palabras)

    detectadas = []
    puntaje = 0

    for palabra in palabras_set & PALABRAS_ALTA_URGENCIA:
        detectadas.append(palabra)
        puntaje += 2

    for palabra in palabras_set & PALABRAS_MEDIA_URGENCIA:
        detectadas.append(palabra)
        puntaje += 1

    num_exclamaciones = texto.count("!") + texto.count("¡")
    if num_exclamaciones >= 2:
        detectadas.append("exclamaciones_multiples")
        puntaje += 2
    elif num_exclamaciones == 1:
        puntaje += 1

    letras = [c for c in texto if c.isalpha()]
    if len(letras) >= 6:
        proporcion_mayus = sum(1 for c in letras if c.isupper()) / len(letras)
        if proporcion_mayus > 0.6:
            detectadas.append("mayusculas_excesivas")
            puntaje += 2

    if puntaje >= 4:
        nivel = "alto"
    elif puntaje >= 2:
        nivel = "medio"
    else:
        nivel = "bajo"

    return {"puntaje": puntaje, "nivel": nivel, "palabras_detectadas": detectadas}