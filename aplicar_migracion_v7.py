"""
aplicar_migracion_v7.py
-------------------------
Renombra el ROL 'recepcionista' a 'mesero' en todo el sistema de datos.

La tabla 'usuarios' tiene un CHECK que solo permitia
    rol IN ('recepcionista','almacenero','cocinero','cajero','admin')
por lo que no basta un UPDATE: hay que RECREAR la tabla con el nuevo CHECK
    rol IN ('mesero','almacenero','cocinero','cajero','admin')
y migrar las filas (recepcionista -> mesero). Se conservan ids, jids,
credenciales y el resto de columnas, de modo que las claves foraneas de
'pedidos' e 'interacciones_estres' (usuario_id) siguen siendo validas.

Idempotente: si el CHECK ya permite 'mesero', no hace nada.

Uso (con el servidor DETENIDO):
    python aplicar_migracion_v7.py
"""
import sqlite3

DB_PATH = "restaurante.db"


def _ya_migrada(con):
    row = con.execute("SELECT sql FROM sqlite_master WHERE name='usuarios'").fetchone()
    return bool(row) and "'mesero'" in row[0]


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    if _ya_migrada(con):
        print("  - Migracion v7 ya aplicada (rol 'mesero' permitido).")
        con.close()
        return

    cur.execute("PRAGMA foreign_keys=OFF")
    cur.execute("BEGIN")
    try:
        # Tabla nueva con el CHECK actualizado (mesero en lugar de recepcionista).
        cur.execute(
            """
            CREATE TABLE usuarios_nueva (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre        TEXT NOT NULL,
                jid           TEXT NOT NULL UNIQUE,
                rol           TEXT NOT NULL CHECK (rol IN ('mesero','almacenero','cocinero','cajero','admin')),
                activo        INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0,1)),
                username      TEXT,
                password_hash TEXT
            )
            """
        )
        # Copiar datos; recepcionista -> mesero al vuelo.
        cur.execute(
            """
            INSERT INTO usuarios_nueva (id, nombre, jid, rol, activo, username, password_hash)
            SELECT id, nombre, jid,
                   CASE WHEN rol = 'recepcionista' THEN 'mesero' ELSE rol END,
                   activo, username, password_hash
            FROM usuarios
            """
        )
        cur.execute("DROP TABLE usuarios")
        cur.execute("ALTER TABLE usuarios_nueva RENAME TO usuarios")
        # Mantener el contador AUTOINCREMENT alineado al mayor id existente.
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM usuarios")
        max_id = cur.fetchone()[0]
        cur.execute("UPDATE sqlite_sequence SET seq=? WHERE name='usuarios'", (max_id,))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('usuarios', ?)", (max_id,))
        con.commit()
    except Exception as e:
        con.rollback()
        con.close()
        print(f"  ! ERROR en la migracion v7, se revirtio: {e}")
        raise

    cur.execute("PRAGMA foreign_keys=ON")
    problemas = cur.execute("PRAGMA foreign_key_check").fetchall()
    n = cur.execute("SELECT COUNT(*) FROM usuarios WHERE rol='mesero'").fetchone()[0]
    con.close()

    print(f"  + Tabla usuarios recreada: rol 'recepcionista' -> 'mesero' ({n} usuarios con rol mesero).")
    if problemas:
        print(f"  ! AVISO: inconsistencias de clave foranea detectadas: {problemas}")
    else:
        print("  + Integridad de claves foraneas OK.")


if __name__ == "__main__":
    main()
