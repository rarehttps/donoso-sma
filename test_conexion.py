"""
test_conexion.py
------------------
Diagnostico minimo: SOLO intenta conectar un agente al servidor XMPP,
sin FastAPI, sin hilos, sin nada mas. Si esto tambien se cuelga, el
problema es de SPADE/pyjabber con tu Windows (no de nuestra app).

Uso:
    Terminal 1: spade run --host 127.0.0.1
    Terminal 2: python test_conexion.py
"""
import asyncio
import time
from spade.agent import Agent


class AgentePrueba(Agent):
    async def setup(self):
        print("[OK] El agente de prueba se conecto correctamente.")


async def main():
    print("Intentando conectar agente de prueba a 127.0.0.1 ...")
    inicio = time.time()
    agente = AgentePrueba("prueba@127.0.0.1", "clave_prueba_dev")
    try:
        await agente.start(auto_register=True)
        print(f"[OK] Conectado en {time.time() - inicio:.1f} segundos.")
        await agente.stop()
    except Exception as e:
        print(f"[ERROR] Fallo tras {time.time() - inicio:.1f} segundos: {e}")


if __name__ == "__main__":
    asyncio.run(main())