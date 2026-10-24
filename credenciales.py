"""
credenciales.py
----------------
JIDs y contrasenas de los 4 agentes, compartidas entre orquestador_sma.py
(que arranca Cocinero, Almacenero, Cajero) y app_mesero.py (que arranca
al Mesero, conectandose al MISMO servidor XMPP).

NOTA DE SEGURIDAD: esto es solo para desarrollo local en tu maquina.
Antes de mostrar el repositorio o subirlo a algun lado, mueve esto a un
archivo .env (con python-dotenv) y agrega .env a tu .gitignore.
"""
CLAVES = {
    "mesero": "clave_mesero_dev",
    "cocinero": "clave_cocinero_dev",
    "almacenero": "clave_almacenero_dev",
    "cajero": "clave_cajero_dev",
}