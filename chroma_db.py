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
"""
import os
import uuid
from datetime import datetime

import chromadb
from langchain_ollama import OllamaEmbeddings

RUTA_CHROMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_data")
NOMBRE_COLECCION = "historial_almacen"

# Usamos Ollama (que ya tienes corriendo localmente) para generar los
# vectores, en vez del modelo en ingles que ChromaDB descarga por defecto.
# Si mas adelante instalas un modelo de embeddings dedicado en Ollama
# (ej: "nomic-embed-text"), solo cambia el nombre aqui.
MODELO_EMBEDDINGS = "llama3"


class OllamaEmbeddingFunction:
    """Adaptador para que ChromaDB use Ollama como generador de embeddings."""
    def __init__(self, model: str = MODELO_EMBEDDINGS):
        self._embeddings = OllamaEmbeddings(model=model)

    def __call__(self, input):
        return self._embeddings.embed_documents(list(input))

    def embed_query(self, input):
        textos = input if isinstance(input, (list, tuple)) else [input]
        return self._embeddings.embed_documents(list(textos))

    def name(self) -> str:
        return f"ollama-{MODELO_EMBEDDINGS}"


_cliente = chromadb.PersistentClient(path=RUTA_CHROMA)
_coleccion = _cliente.get_or_create_collection(
    name=NOMBRE_COLECCION,
    embedding_function=OllamaEmbeddingFunction(),
)


def registrar_evento(texto: str, tipo: str, insumo: str | None = None) -> str:
    """
    Guarda un evento en el historial. tipo: 'alerta_stock_bajo',
    'alerta_vencimiento', 'reabastecimiento', 'observacion', etc.
    Devuelve el id generado para el evento.
    """
    evento_id = str(uuid.uuid4())
    metadata = {
        "tipo": tipo,
        "fecha": datetime.now().isoformat(timespec="seconds"),
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