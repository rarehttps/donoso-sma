"""
aplicar_migracion_v6.py
-------------------------
Agrega la columna 'usuario_id' a la tabla interacciones_estres para poder
atribuir la CARGA DE TRABAJO MENTAL a cada trabajador (usuario) del sistema.

Con esto el panel de administracion puede mostrar la carga mental por
trabajador (grafico de pastel por usuario) y la evolucion/mejora en el tiempo.

Idempotente: si la columna ya existe, no hace nada.

Uso (con el servidor DETENIDO):
    python aplicar_migracion_v6.py
"""
import sqlite3

DB_PATH = "restaurante.db"

COLUMNAS = [("usuario_id", "INTEGER")]


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("PRAGMA table_info(interacciones_estres)")
    existentes = {row[1] for row in cur.fetchall()}
    for nombre, tipo in COLUMNAS:
        if nombre in existentes:
            print(f"  - Columna '{nombre}' ya existe, se omite.")
        else:
            cur.execute(f"ALTER TABLE interacciones_estres ADD COLUMN {nombre} {tipo}")
            print(f"  + Columna '{nombre}' agregada.")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_estres_usuario ON interacciones_estres(usuario_id)"
    )
    con.commit()
    con.close()
    print("Migracion v6 aplicada correctamente.")


if __name__ == "__main__":
    main()
