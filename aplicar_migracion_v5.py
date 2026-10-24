"""
aplicar_migracion_v5.py
-------------------------
Agrega las columnas de autenticacion a la tabla usuarios:
  - username       (nombre de acceso)
  - password_hash  (contrasena cifrada con bcrypt)

Idempotente. El sembrado de credenciales por defecto (usuarios demo + admin)
lo hace db_utils.sembrar_credenciales_demo() al arrancar la app.

Uso:
    python aplicar_migracion_v5.py
"""
import sqlite3

DB_PATH = "restaurante.db"

COLUMNAS = [("username", "TEXT"), ("password_hash", "TEXT")]


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("PRAGMA table_info(usuarios)")
    existentes = {row[1] for row in cur.fetchall()}
    for nombre, tipo in COLUMNAS:
        if nombre in existentes:
            print(f"  - Columna '{nombre}' ya existe, se omite.")
        else:
            cur.execute(f"ALTER TABLE usuarios ADD COLUMN {nombre} {tipo}")
            print(f"  + Columna '{nombre}' agregada.")
    con.commit()
    con.close()
    print("Migracion v5 aplicada correctamente.")


if __name__ == "__main__":
    main()
