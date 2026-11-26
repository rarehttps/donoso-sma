"""
aplicar_migracion_v8.py
-------------------------
Crea la tabla 'alertas_sobrecarga': bitacora de las ALERTAS DE SOBRECARGA
(rol afectado, % de carga alta, trabajador mas cargado, pedidos que se
priorizaron en cocina y la sugerencia dada). Asi queda evidencia de cada
vez que el sistema detecto sobrecarga y actuo.

Idempotente: si la tabla ya existe, no hace nada.

Uso (con el servidor DETENIDO):
    python aplicar_migracion_v8.py
"""
import sqlite3

import proteccion_datos

DB_PATH = "restaurante.db"


def main():
    con = proteccion_datos.conectar(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS alertas_sobrecarga (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha_hora          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            rol                 TEXT NOT NULL,
            nivel               TEXT NOT NULL CHECK (nivel IN ('alta','critica')),
            lecturas            INTEGER NOT NULL,
            pct_alta            REAL NOT NULL,
            trabajador_id       INTEGER,
            trabajador          TEXT,
            pedidos_priorizados TEXT,
            acciones            TEXT,
            sugerencia          TEXT,
            atendida            INTEGER NOT NULL DEFAULT 0 CHECK (atendida IN (0,1)),
            FOREIGN KEY (trabajador_id) REFERENCES usuarios(id) ON DELETE SET NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alertas_fecha ON alertas_sobrecarga(fecha_hora)")
    con.commit()
    con.close()
    print("Migracion v8 aplicada correctamente (bitacora de alertas de sobrecarga).")


if __name__ == "__main__":
    main()
