"""
menu_rag.py
------------
RAG (Retrieval-Augmented Generation) del MENU, para responder consultas
sobre ingredientes y alergenos ("¿el Superoso tiene mani?", "¿algo sin
gluten?", "¿que lleva la donoso?").

Es una coleccion de ChromaDB SEPARADA del historial de almacen
(chroma_db.py): aqui se indexan los platos de la carta —descripcion +
ingredientes reales tomados de receta_insumo— y el Agente Mesero recupera
por similitud semantica los platos relevantes ANTES de que Llama redacte
la respuesta. Asi el modelo responde con datos reales del negocio y no
alucina ingredientes.

A diferencia del historial de almacen (que usa llama3 como embedder
provisional), aqui usamos 'nomic-embed-text', un modelo dedicado a
embeddings: mas rapido y con mejor recuperacion. Descargalo una vez con:
    ollama pull nomic-embed-text
"""
import os

import chromadb
from langchain_ollama import OllamaEmbeddings

RUTA_CHROMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_data")
NOMBRE_COLECCION = "menu_alergenos"
MODELO_EMBEDDINGS = "nomic-embed-text"


class OllamaEmbeddingFunction:
    """Adaptador para que ChromaDB genere embeddings con Ollama local."""
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


def _documento_de_plato(p: dict) -> str:
    """Construye el texto indexable de un plato a partir de su descripcion
    y de sus ingredientes reales. Es lo que se compara semanticamente y lo
    que se le entrega al LLM como contexto."""
    partes = [f"{p['nombre']} (categoria {p.get('categoria', '')}, S/ {float(p['precio']):.2f})."]
    if p.get("descripcion"):
        partes.append(f"Descripcion: {p['descripcion']}.")
    if p.get("ingredientes"):
        partes.append(f"Ingredientes: {p['ingredientes']}.")
    return " ".join(partes)


def reindexar_menu(platos: list[dict]) -> int:
    """
    Indexa (o reindexa) todos los platos en la coleccion. Es IDEMPOTENTE:
    usa el id del plato como id del documento y hace upsert, asi que se
    puede llamar en cada arranque sin duplicar nada.

    platos: lista de dicts de db.obtener_menu_para_rag()
    Devuelve la cantidad de platos indexados.
    """
    if not platos:
        return 0

    ids = [f"plato-{p['id']}" for p in platos]
    documentos = [_documento_de_plato(p) for p in platos]
    metadatos = [
        {
            "plato": p["nombre"],
            "categoria": p.get("categoria", ""),
            "precio": float(p["precio"]),
        }
        for p in platos
    ]

    _coleccion.upsert(ids=ids, documents=documentos, metadatas=metadatos)
    return len(platos)


def buscar_platos(consulta: str, n_resultados: int = 3) -> list[dict]:
    """
    Recupera los platos mas relevantes a la consulta por similitud
    semantica. Devuelve [{texto, plato, categoria, precio}], o [] si la
    coleccion esta vacia.
    """
    total = _coleccion.count()
    if total == 0:
        return []

    resultados = _coleccion.query(
        query_texts=[consulta],
        n_results=min(n_resultados, total),
    )

    documentos = resultados.get("documents", [[]])[0]
    metadatas = resultados.get("metadatas", [[]])[0]
    salida = []
    for doc, meta in zip(documentos, metadatas):
        salida.append({"texto": doc, **(meta or {})})
    return salida


def contar() -> int:
    """Cantidad de platos actualmente indexados."""
    return _coleccion.count()
