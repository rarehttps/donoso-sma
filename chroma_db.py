"""
chroma_db.py
-------------
Base de datos vectorial (ChromaDB) para el HISTORIAL DE ALMACEN.

Mientras restaurante.db (SQLite) guarda el ESTADO ACTUAL (cuanto stock
hay ahora), ChromaDB guarda EVENTOS pasados en lenguaje natural (alertas,
reabastecimientos, observaciones) para que los agentes puedan responder
preguntas como "que paso con la carne esta semana" usando busqueda
semantica, no solo filtros exactos de fecha.

Esto es lo que le da al sistema su componente RAG (Retrieval-Augmented
Generation): antes de que Ollama responda, se recupera contexto real
y reciente del almacen para que la respuesta no dependa solo de lo que
el modelo "recuerda" de su entrenamiento.

OPTIMIZACION DE LATENCIA:
  - Los vectores se generan con 'nomic-embed-text' (modelo dedicado a
    embeddings, el mismo del RAG del menu), no con el LLM de chat. Antes se
    usaba llama3 (8B): cada consulta obligaba a Ollama a cargar ese modelo
    en RAM ademas del modelo de chat, y eso frenaba las respuestas.
  - La coleccion antigua (vectores de llama3) se conserva intacta y sus
    eventos se copian una sola vez a la nueva, sin duplicados.
  - Si nomic-embed-text no esta descargado, se sigue usando la coleccion
    antigua con llama3 (el sistema no se rompe).
  - Un mismo evento (mismo texto) se registra como maximo una vez por dia.
"""
import hashlib
import os
from datetime import datetime

import chromadb
from langchain_ollama import OllamaEmbeddings

RUTA_CHROMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_data")
MODELO_EMBEDDINGS = "nomic-embed-text"
NOMBRE_COLECCION = "historial_almacen_nomic"
MODELO_ANTERIOR = "llama3"
NOMBRE_COLECCION_ANTERIOR = "historial_almacen"


class OllamaEmbeddingFunction:
    """Adaptador para que ChromaDB use Ollama como generador de embeddings."""
    def __init__(self, model: str = MODELO_EMBEDDINGS):
        self._modelo = model
        self._embeddings = OllamaEmbeddings(model=model)

    def __call__(self, input):
        return self._embeddings.embed_documents(list(input))

    def embed_query(self, input):
        textos = input if isinstance(input, (list, tuple)) else [input]
        return self._embeddings.embed_documents(list(textos))

    def name(self) -> str:
        return f"ollama-{self._modelo}"


def _modelo_descargado(nombre: str) -> bool | None:
    """True/False si se pudo consultar a Ollama; None si Ollama no responde."""
    try:
        import ollama
        resp = ollama.list()
        modelos = getattr(resp, "models", None)
        if modelos is None:
            modelos = resp.get("models", [])
        nombres = [str(getattr(m, "model", None) or m.get("model") or m.get("name") or "") for m in modelos]
        return any(n == nombre or n.startswith(nombre + ":") for n in nombres)
    except Exception:
        return None


_cliente = chromadb.PersistentClient(path=RUTA_CHROMA)

# Si nomic-embed-text no esta (o no se puede verificar porque Ollama aun no
# arranca) se intenta igual con nomic: el menu RAG ya lo necesita. Solo si
# Ollama confirma que NO esta descargado, se usa la coleccion antigua.
if _modelo_descargado(MODELO_EMBEDDINGS) is False:
    print(f"[chroma_db] '{MODELO_EMBEDDINGS}' no esta descargado: el historial usa {MODELO_ANTERIOR} "
          f"(mas lento). Para acelerarlo: ollama pull {MODELO_EMBEDDINGS}")
    MODELO_ACTIVO, COLECCION_ACTIVA = MODELO_ANTERIOR, NOMBRE_COLECCION_ANTERIOR
else:
    MODELO_ACTIVO, COLECCION_ACTIVA = MODELO_EMBEDDINGS, NOMBRE_COLECCION

_coleccion = _cliente.get_or_create_collection(
    name=COLECCION_ACTIVA,
    embedding_function=OllamaEmbeddingFunction(MODELO_ACTIVO),
)


def _id_evento(texto: str, dia: str) -> str:
    return "ev-" + hashlib.sha1(f"{dia}|{texto}".encode("utf-8")).hexdigest()[:24]


def migrar_historial_anterior() -> int:
    """Copia UNA VEZ los eventos de la coleccion antigua (llama3) a la nueva
    (nomic-embed-text), sin duplicados. La antigua no se borra.
    Devuelve cuantos eventos se copiaron."""
    if COLECCION_ACTIVA == NOMBRE_COLECCION_ANTERIOR or _coleccion.count() > 0:
        return 0
    try:
        vieja = _cliente.get_collection(
            NOMBRE_COLECCION_ANTERIOR, embedding_function=OllamaEmbeddingFunction(MODELO_ANTERIOR))
    except Exception:
        return 0  # no habia historial anterior
    datos = vieja.get(include=["documents", "metadatas"])
    unicos = {}
    for doc, meta in zip(datos.get("documents") or [], datos.get("metadatas") or []):
        if not doc:
            continue
        meta = dict(meta or {})
        dia = str(meta.get("fecha", ""))[:10]
        unicos[_id_evento(doc, dia)] = (doc, meta)     # mismo texto el mismo dia = un evento
    ids = list(unicos)
    for i in range(0, len(ids), 64):
        lote = ids[i:i + 64]
        _coleccion.add(ids=lote, documents=[unicos[k][0] for k in lote],
                       metadatas=[unicos[k][1] for k in lote])
    return len(ids)


def registrar_evento(texto: str, tipo: str, insumo: str | None = None) -> str:
    """
    Guarda un evento en el historial. tipo: 'alerta_stock_bajo',
    'alerta_vencimiento', 'reabastecimiento', 'observacion', etc.
    Devuelve el id del evento. Si el mismo evento ya se registro hoy, no lo
    duplica (ni vuelve a calcular su vector).
    """
    ahora = datetime.now()
    evento_id = _id_evento(texto, ahora.date().isoformat())
    if _coleccion.get(ids=[evento_id], include=[])["ids"]:
        return evento_id
    metadata = {
        "tipo": tipo,
        "fecha": ahora.isoformat(timespec="seconds"),
    }
    if insumo:
        metadata["insumo"] = insumo

    _coleccion.add(documents=[texto], metadatas=[metadata], ids=[evento_id])
    return evento_id


def buscar_eventos_similares(consulta: str, n_resultados: int = 3) -> list[dict]:
    """
    Busqueda semantica sobre el historial. Devuelve los eventos mas
    relevantes a la consulta, sin importar si usan las mismas palabras.
    """
    total = _coleccion.count()
    if total == 0:
        return []

    resultados = _coleccion.query(
        query_texts=[consulta],
        n_results=min(n_resultados, total),
    )

    eventos = []
    documentos = resultados.get("documents", [[]])[0]
    metadatas = resultados.get("metadatas", [[]])[0]
    for doc, meta in zip(documentos, metadatas):
        eventos.append({"texto": doc, **meta})
    return eventos
