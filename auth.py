"""
auth.py
--------
Autenticacion y control de acceso por ROLES del sistema.

- Contrasenas cifradas con bcrypt (nunca se guardan en texto plano).
- Sesiones en memoria: al iniciar sesion se genera un token aleatorio que
  viaja en una cookie httponly; el servidor mapea token -> usuario. Es
  suficiente para un despliegue local de un solo proceso (se pierden al
  reiniciar, y el usuario simplemente vuelve a entrar).
- Cada ROL define a que PANELES puede entrar (control de acceso).

Roles del sistema (coinciden con el CHECK de la tabla usuarios):
  admin, mesero, cocinero, almacenero, cajero.
"""
import secrets

import bcrypt

# ---------------------------------------------------------------
# Contrasenas
# ---------------------------------------------------------------
def hashear(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verificar(password: str, hash_guardado: str) -> bool:
    if not hash_guardado:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hash_guardado.encode("utf-8"))
    except Exception:
        return False


# ---------------------------------------------------------------
# Sesiones en memoria (token -> datos del usuario)
# ---------------------------------------------------------------
_sesiones: dict[str, dict] = {}


def crear_sesion(usuario: dict) -> str:
    token = secrets.token_urlsafe(32)
    _sesiones[token] = {
        "id": usuario["id"],
        "nombre": usuario["nombre"],
        "rol": usuario["rol"],
        "username": usuario.get("username"),
    }
    return token


def obtener_sesion(token: str | None) -> dict | None:
    return _sesiones.get(token) if token else None


def eliminar_sesion(token: str | None):
    if token:
        _sesiones.pop(token, None)


# ---------------------------------------------------------------
# Control de acceso por rol
# ---------------------------------------------------------------
# Que paneles ve cada rol. 'mesero' = panel del Mesero (ruta '/').
ACCESO = {
    "admin":         ["mesero", "cocina", "almacen", "caja", "sla", "monitor", "admin"],
    "mesero": ["mesero"],
    "cocinero":      ["cocina"],
    "almacenero":    ["almacen"],
    "cajero":        ["caja"],
}

# A donde se lo envia justo despues de iniciar sesion.
LANDING = {
    "admin":         "/admin",
    "mesero": "/",
    "cocinero":      "/cocina",
    "almacenero":    "/almacen",
    "cajero":        "/caja",
}


def paneles_de(rol: str) -> list[str]:
    return ACCESO.get(rol, [])


def puede_ver(rol: str, panel: str) -> bool:
    return panel in ACCESO.get(rol, [])


def landing(rol: str) -> str:
    return LANDING.get(rol, "/login")
