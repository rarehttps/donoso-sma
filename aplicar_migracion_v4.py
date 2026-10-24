"""
aplicar_migracion_v4.py
-------------------------
Agrega la columna 'motivo_anulacion' a la tabla pedidos, para poder
GUARDAR el motivo cuando un pedido ya cobrado se anula (devolucion /
reembolso). Antes ese motivo se recibia pero se perdia.

Idempotente: se puede correr varias veces sin problema.

Uso:
    python aplicar_migracion_v4.py
"""
import sqlite3

DB_PATH = "restaurante.db"


def main():
    conexion = sqlite3.connect(DB_PATH)
    cur = conexion.cursor()

    cur.execute("PRAGMA table_info(pedidos)")
    columnas = {row[1] for row in cur.fetchall()}

    if "motivo_anulacion" in columnas:
        print("  - Columna 'motivo_anulacion' ya existe, se omite.")
    else:
        cur.execute("ALTER TABLE pedidos ADD COLUMN motivo_anulacion TEXT")
        print("  + Columna 'motivo_anulacion' agregada.")

    conexion.commit()
    conexion.close()
    print("Migracion v4 aplicada correctamente. Tus datos existentes no se tocaron.")


if __name__ == "__main__":
    main()
