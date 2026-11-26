"""
proteccion_datos.py
-------------------
Proteccion de datos de DonOso SMA: CIFRADO EN REPOSO de restaurante.db.

Que hace
  - Cifra la base completa con SQLCipher 4 (AES-256 por pagina, autenticado con
    HMAC-SHA512). Sin la clave el archivo es ilegible: no se puede abrir con DB
    Browser, con sqlite3, ni copiandolo a otra computadora.
  - La clave (256 bits) vive en el archivo .env como SMA_DB_KEY: fuera del codigo
    y fuera del repositorio (.env esta en .gitignore).
  - Es transparente: db_utils (aiosqlite) y las migraciones usan las mismas
    consultas de siempre; esta capa solo agrega la clave al abrir restaurante.db.
  - La primera vez que el sistema arranca con SQLCipher instalado, cifra la base
    existente: crea una copia cifrada, comprueba que contenga exactamente los
    mismos datos y recien entonces reemplaza al archivo original.
  - Degradacion segura: si SQLCipher no esta instalado, el sistema sigue
    funcionando (sin cifrado) y lo avisa en consola.

Instalacion (una sola vez, dentro de env_tesis):
    pip install sqlcipher3-wheels

Se importa PRIMERO en app_principal.py.
"""
import hashlib
import os
import secrets
import sqlite3 as _sqlite_std
import types

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "restaurante.db")
ENV_PATH = os.path.join(BASE_DIR, ".env")
_NOMBRE_BD = "restaurante.db"
_CABECERA_PLANA = b"SQLite format 3\x00"

try:
    import sqlcipher3.dbapi2 as _motor
except ImportError:
    _motor = None

ESTADO = "sin_iniciar"      # 'cifrada' | 'sin_cifrar'
_pragma_clave = None


def _es_bd_protegida(database) -> bool:
    try:
        return os.path.basename(os.fspath(database)).lower() == _NOMBRE_BD
    except TypeError:
        return False


def _es_texto_plano(ruta) -> bool:
    with open(ruta, "rb") as f:
        return f.read(16) == _CABECERA_PLANA


def _leer_clave(permitir_generar: bool) -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        pass
    clave = os.environ.get("SMA_DB_KEY", "").strip()
    if not clave:
        if not permitir_generar:
            raise RuntimeError(
                "restaurante.db esta cifrada pero no se encontro SMA_DB_KEY en .env. "
                "Restaura el .env con la clave original: sin ella los datos no se pueden leer.")
        clave = secrets.token_hex(32)
        with open(ENV_PATH, "a", encoding="utf-8") as f:
            f.write("\n# Clave de cifrado de restaurante.db (AES-256). NO compartir ni subir a git.\n"
                    "# Guarda una copia segura: sin ella la base no se puede abrir.\n"
                    f"SMA_DB_KEY={clave}\n")
        os.environ["SMA_DB_KEY"] = clave
        print("[proteccion] Se genero una clave de cifrado nueva en .env (guarda una copia segura).")
    if len(clave) != 64 or any(c not in "0123456789abcdefABCDEF" for c in clave):
        raise RuntimeError("SMA_DB_KEY en .env debe tener 64 caracteres hexadecimales (clave de 256 bits).")
    return clave


def _conectar_motor(database, *args, **kwargs):
    """Conexion SQLCipher; a restaurante.db le aplica la clave."""
    con = _motor.connect(database, *args, **kwargs)
    if _pragma_clave and _es_bd_protegida(database):
        con.execute(_pragma_clave)
    return con


def conectar(database=DB_PATH, **kwargs):
    """Conexion sincronica para scripts (migraciones). Usa la clave si la base esta cifrada."""
    if ESTADO == "cifrada":
        return _conectar_motor(database, **kwargs)
    return _sqlite_std.connect(database, **kwargs)


def _huella(con) -> str:
    """Resumen de TODO el contenido (esquema + filas de cada tabla)."""
    h = hashlib.sha256()
    for fila in con.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name"):
        h.update(repr(fila).encode())
    tablas = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in tablas:
        h.update(t.encode())
        for fila in con.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
            h.update(repr(tuple(fila)).encode())
    return h.hexdigest()


def _cifrar_base_existente(clave: str):
    tmp = DB_PATH + ".cifrando"
    if os.path.exists(tmp):
        os.remove(tmp)
    plano = _motor.connect(DB_PATH)                 # sin clave: lee el archivo plano
    try:
        huella_original = _huella(plano)
        version = plano.execute("PRAGMA user_version").fetchone()[0]
        ruta = tmp.replace("'", "''")
        plano.execute(f"ATTACH DATABASE '{ruta}' AS cifrada KEY \"x'{clave}'\"")
        plano.execute("SELECT sqlcipher_export('cifrada')")
        plano.execute(f"PRAGMA cifrada.user_version = {int(version)}")
        plano.execute("DETACH DATABASE cifrada")
    finally:
        plano.close()

    nueva = _motor.connect(tmp)
    try:
        nueva.execute(f"PRAGMA key = \"x'{clave}'\"")
        integra = nueva.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        identica = _huella(nueva) == huella_original
    finally:
        nueva.close()
    if not (integra and identica):
        os.remove(tmp)
        raise RuntimeError("la copia cifrada no coincide con la original; no se reemplazo nada")
    try:
        os.replace(tmp, DB_PATH)
    except OSError:
        os.remove(tmp)
        raise RuntimeError("restaurante.db esta abierta en otro programa; cierralo y reinicia")


def _enganchar_aiosqlite():
    """Hace que aiosqlite (usado por db_utils) abra la base con SQLCipher."""
    try:
        import aiosqlite
        import aiosqlite.core
    except ImportError:
        return
    shim = types.ModuleType("sqlite3_protegido")
    for nombre in dir(_motor):
        if not nombre.startswith("__"):
            setattr(shim, nombre, getattr(_motor, nombre))
    shim.connect = _conectar_motor
    aiosqlite.core.sqlite3 = shim
    for nombre in ("Row", "Error", "DatabaseError", "IntegrityError", "OperationalError",
                   "ProgrammingError", "NotSupportedError", "InterfaceError", "DataError",
                   "InternalError", "Warning"):
        if hasattr(aiosqlite, nombre) and hasattr(_motor, nombre):
            setattr(aiosqlite, nombre, getattr(_motor, nombre))


def _inicializar():
    global ESTADO, _pragma_clave
    existe = os.path.exists(DB_PATH)
    plana = existe and _es_texto_plano(DB_PATH)

    if _motor is None:
        if existe and not plana:
            raise RuntimeError("restaurante.db esta cifrada y falta SQLCipher. "
                               "Instala dentro de env_tesis: pip install sqlcipher3-wheels")
        ESTADO = "sin_cifrar"
        print("[proteccion] AVISO: SQLCipher no esta instalado; restaurante.db NO esta cifrada. "
              "Instala: pip install sqlcipher3-wheels")
        return

    clave = _leer_clave(permitir_generar=(plana or not existe))
    if plana:
        try:
            _cifrar_base_existente(clave)
            print("[proteccion] restaurante.db cifrada con AES-256 (SQLCipher). Los datos no cambiaron.")
        except Exception as e:
            ESTADO = "sin_cifrar"
            print(f"[proteccion] AVISO: no se pudo cifrar la base ({e}); se continua sin cifrado.")
            return

    _pragma_clave = f"PRAGMA key = \"x'{clave}'\""
    if existe:
        con = _motor.connect(DB_PATH)
        try:
            con.execute(_pragma_clave)
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except _motor.DatabaseError:
            raise RuntimeError("La clave SMA_DB_KEY de .env no corresponde a restaurante.db.") from None
        finally:
            con.close()
    _enganchar_aiosqlite()
    ESTADO = "cifrada"
    print("[proteccion] Base de datos protegida: cifrado en reposo AES-256 activo.")


def estado() -> dict:
    return {"estado": ESTADO, "motor": "SQLCipher (AES-256)" if ESTADO == "cifrada" else "SQLite sin cifrar"}


_inicializar()
