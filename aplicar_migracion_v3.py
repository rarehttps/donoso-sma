"""
aplicar_migracion_v3.py
-------------------------
Aplica la migracion v3 a restaurante.db de forma SEGURA: agrega
columnas de tiempos (para SLA) y la tabla de interacciones de estres,
sin tocar ni borrar los datos que ya tienes (carta, insumos, etc).

Se puede correr varias veces sin problema (detecta que cambios ya
estan aplicados y los salta).

Uso:
    python aplicar_migracion_v3.py
"""
import sqlite3

DB_PATH = "restaurante.db"

COLUMNAS_NUEVAS = [
    ("fecha_inicio_preparacion", "TEXT"),
    ("fecha_listo", "TEXT"),
    ("fecha_pagado", "TEXT"),
    ("fecha_cancelado", "TEXT"),
]


def main():
    conexion = sqlite3.connect(DB_PATH)
    cur = conexion.cursor()

    cur.execute("PRAGMA table_info(pedidos)")
    columnas_actuales = {row[1] for row in cur.fetchall()}

    for nombre, tipo in COLUMNAS_NUEVAS:
        if nombre in columnas_actuales:
            print(f"  - Columna '{nombre}' ya existe, se omite.")
        else:
            cur.execute(f"ALTER TABLE pedidos ADD COLUMN {nombre} {tipo}")
            print(f"  + Columna '{nombre}' agregada.")

    cur.execute('''
        CREATE TABLE IF NOT EXISTS interacciones_estres (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mensaje         TEXT NOT NULL,
            puntaje         INTEGER NOT NULL,
            nivel           TEXT NOT NULL,
            palabras_clave  TEXT,
            fecha_hora      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_estres_fecha ON interacciones_estres(fecha_hora)")
    print("  + Tabla 'interacciones_estres' lista.")

    conexion.commit()
    conexion.close()
    print("Migracion v3 aplicada correctamente. Tu carta, insumos y pedidos existentes no se tocaron.")


if __name__ == "__main__":
    main()