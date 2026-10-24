"""
agent_runtime.py
------------------
Runtime de los agentes SPADE con TOLERANCIA A FALLOS.

1. AISLAMIENTO DEL EVENT LOOP: SPADE administra su propio loop. En Windows
   puede no coincidir con el de uvicorn, rompiendo la sesion XMPP. Por eso
   los agentes corren en un HILO dedicado con su propio loop.

2. SUPERVISION Y REINICIO (patron Supervisor): un lazo de supervision
   consulta el heartbeat de cada agente (Agent.is_alive()). Si un agente
   cae, lo reinicia automaticamente con backoff exponencial. Implementa el
   patron de monitoreo + recuperacion de la literatura de sistemas
   multiagente. El estado queda expuesto en /api/salud y /metrics.
"""
import asyncio
import threading
from datetime import datetime

from agentes import MeseroAgent, CocineroAgent, AlmaceneroAgent, CajeroAgent
from credenciales import CLAVES

ESPECIFICACIONES = {
    "cocinero":      (CocineroAgent,      "cocinero@127.0.0.1",      CLAVES["cocinero"]),
    "almacenero":    (AlmaceneroAgent,    "almacenero@127.0.0.1",    CLAVES["almacenero"]),
    "cajero":        (CajeroAgent,        "cajero@127.0.0.1",        CLAVES["cajero"]),
    "mesero": (MeseroAgent, "mesero@127.0.0.1", CLAVES["mesero"]),
}

INTERVALO_HEARTBEAT = 5.0
MAX_REINTENTOS = 5
BACKOFF_BASE = 2.0

agentes = {nombre: None for nombre in ESPECIFICACIONES}

salud = {
    nombre: {
        "vivo": False,
        "reinicios": 0,
        "ultimo_chequeo": None,
        "ultimo_reinicio": None,
        "fallo_definitivo": False,
    }
    for nombre in ESPECIFICACIONES
}

_loop: asyncio.AbstractEventLoop | None = None
_agentes_listos = threading.Event()
_error_arranque: Exception | None = None
_supervisor_activo = True


async def _crear_y_arrancar(nombre: str):
    Clase, jid, clave = ESPECIFICACIONES[nombre]
    agente = Clase(jid, clave)
    await agente.start(auto_register=True)
    agentes[nombre] = agente
    salud[nombre]["vivo"] = True
    salud[nombre]["fallo_definitivo"] = False
    return agente


async def _arrancar_agentes():
    global _error_arranque
    try:
        for nombre in ("cocinero", "almacenero", "cajero", "mesero"):
            await _crear_y_arrancar(nombre)
        print("[agent_runtime] Los 4 agentes estan conectados al servidor XMPP.")
    except Exception as e:
        _error_arranque = e
        print(f"[agent_runtime] ERROR al arrancar los agentes: {e}")
    finally:
        _agentes_listos.set()


async def _reiniciar_agente(nombre: str):
    info = salud[nombre]
    for intento in range(1, MAX_REINTENTOS + 1):
        espera = BACKOFF_BASE ** (intento - 1)
        print(f"[supervisor] Reiniciando '{nombre}' (intento {intento}/{MAX_REINTENTOS}, espera {espera:.0f}s)...")
        await asyncio.sleep(espera)
        try:
            anterior = agentes.get(nombre)
            if anterior is not None:
                try:
                    await anterior.stop()
                except Exception:
                    pass
            await _crear_y_arrancar(nombre)
            info["reinicios"] += 1
            info["ultimo_reinicio"] = datetime.now().isoformat(timespec="seconds")
            print(f"[supervisor] Agente '{nombre}' reiniciado correctamente.")
            return True
        except Exception as e:
            print(f"[supervisor] Fallo el reinicio de '{nombre}': {e}")
    info["fallo_definitivo"] = True
    print(f"[supervisor] '{nombre}' no se recupero tras {MAX_REINTENTOS} intentos. Requiere intervencion humana.")
    return False


async def _supervisor():
    print(f"[supervisor] Activo. Chequeo de salud cada {INTERVALO_HEARTBEAT:.0f}s.")
    while _supervisor_activo:
        await asyncio.sleep(INTERVALO_HEARTBEAT)
        for nombre, agente in list(agentes.items()):
            info = salud[nombre]
            info["ultimo_chequeo"] = datetime.now().isoformat(timespec="seconds")
            if info["fallo_definitivo"]:
                continue
            vivo = bool(agente) and agente.is_alive()
            info["vivo"] = vivo
            if not vivo:
                print(f"[supervisor] Agente '{nombre}' NO responde. Iniciando recuperacion...")
                await _reiniciar_agente(nombre)


def _hilo_agentes():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.create_task(_arrancar_agentes())
    _loop.create_task(_supervisor())
    _loop.run_forever()


def iniciar(timeout: float = 40.0):
    hilo = threading.Thread(target=_hilo_agentes, daemon=True, name="spade-agentes")
    hilo.start()
    if not _agentes_listos.wait(timeout):
        raise RuntimeError(
            "Los agentes no terminaron de conectarse. "
            "Verifica que 'spade run --host 127.0.0.1' este corriendo."
        )
    if _error_arranque:
        raise _error_arranque


def detener():
    global _supervisor_activo
    _supervisor_activo = False
    if _loop and any(agentes.values()):
        async def _detener_todos():
            for ag in agentes.values():
                if ag:
                    try:
                        await ag.stop()
                    except Exception:
                        pass
        future = asyncio.run_coroutine_threadsafe(_detener_todos(), _loop)
        try:
            future.result(timeout=10)
        except Exception as e:
            print(f"[agent_runtime] Aviso al detener agentes: {e}")
        _loop.call_soon_threadsafe(_loop.stop)


def agente_disponible(nombre: str) -> bool:
    ag = agentes.get(nombre)
    return bool(ag) and ag.is_alive()


def estado_salud() -> dict:
    return {
        "agentes": {
            nombre: {**info, "vivo": agente_disponible(nombre)}
            for nombre, info in salud.items()
        },
        "supervisor_activo": _supervisor_activo,
        "intervalo_heartbeat_seg": INTERVALO_HEARTBEAT,
    }


async def ejecutar(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return await asyncio.wrap_future(future)