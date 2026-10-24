"""
cargar_menu_rag.py
-------------------
Indexa el menu de restaurante.db en la coleccion vectorial del RAG
(menu_alergenos) y hace una prueba rapida de recuperacion.

Se puede correr cuando quieras (es idempotente): tras editar la carta,
o simplemente para dejar el RAG listo antes de una demo.

REQUISITOS:
  - Ollama corriendo.
  - Modelo de embeddings descargado:  ollama pull nomic-embed-text

Uso:
    python cargar_menu_rag.py
"""
import asyncio

import db_utils as db
import menu_rag


async def main():
    platos = await db.obtener_menu_para_rag()
    if not platos:
        print("No hay platos disponibles en la BD. Nada que indexar.")
        return

    indexados = menu_rag.reindexar_menu(platos)
    print(f"[OK] {indexados} platos indexados en la coleccion '{menu_rag.NOMBRE_COLECCION}'.")
    print(f"     Total actual en la coleccion: {menu_rag.contar()}\n")

    # Prueba rapida de recuperacion semantica.
    consultas = [
        "el superoso tiene mani?",
        "algo sin gluten",
        "que lleva la donoso",
        "platos con queso",
    ]
    for q in consultas:
        print(f"Consulta: {q!r}")
        resultados = menu_rag.buscar_platos(q, n_resultados=2)
        if not resultados:
            print("   (sin resultados)")
        for r in resultados:
            print(f"   -> {r.get('plato', '?')}: {r['texto'][:90]}...")
        print()


if __name__ == "__main__":
    asyncio.run(main())
