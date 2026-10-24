"""
agentes.py
-----------
Los 4 agentes SPADE. IMPORTANTE: a partir de esta version, NINGUN agente
actua por timers automaticos (asyncio.sleep). Cada transicion de estado
del pedido ocurre cuando un trabajador hace clic en su panel web, lo que
dispara el behaviour correspondiente. Esto es intencional para la tesis:
los tiempos reales entre clic y clic son el dato que se necesita medir
para la carga de trabajo temporal (CarMen-Q).

Flujo de estados de un pedido:
  pendiente -> [Cocina: Iniciar] -> en_preparacion
             -> [Cocina: Completar] -> listo (descuenta stock, avisa a Cajero/Almacenero)
             -> [Caja: Cobrar] -> pagado
El Almacenero revisa el stock de forma independiente (boton "Revisar ahora"
en su panel), no bloquea el flujo del pedido.
"""
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour
from spade.message import Message
from spade.template import Template

import db_utils as db
import chroma_db
import monitor

# Pedidos marcados como URGENTES (por estres alto del cliente en el chat).
# Lo llena el Cocinero al recibir la senal por XMPP y lo lee el panel de
# Cocina. Es un set en memoria compartido entre el hilo de agentes y la API
# (add/discard sobre un set son operaciones atomicas en CPython).
PEDIDOS_URGENTES = set()


# ==============================================================
# MESERO: registra el pedido y avisa a Cocinero (queda 'pendiente')
# ==============================================================
class MeseroAgent(Agent):
    class TomarPedidoBehav(OneShotBehaviour):
        def __init__(self, items, usuario_id=None):
            super().__init__()
            self.items = items
            self.usuario_id = usuario_id

        async def run(self):
            self.pedido_id = await db.crear_pedido(self.usuario_id, self.items)
            pedido_id = self.pedido_id
            print(f"[Mesero] Pedido #{pedido_id} registrado: {self.items}")

            msg = Message(to="cocinero@127.0.0.1")
            msg.set_metadata("performative", "request")
            msg.set_metadata("accion", "preparar_pedido")
            msg.body = db.to_json({"pedido_id": pedido_id})
            await self.send(msg)
            print(f"[Mesero] Pedido #{pedido_id} notificado a Cocinero")
            monitor.registrar_actividad_agente("mesero", "Tomo el pedido y aviso a Cocina",
                                                f"#{pedido_id}: {self.items}")

    class AgregarItemsBehav(OneShotBehaviour):
        def __init__(self, pedido_id, items):
            super().__init__()
            self.pedido_id = pedido_id
            self.items = items

        async def run(self):
            await db.agregar_items_pedido(self.pedido_id, self.items)
            print(f"[Mesero] Items agregados al pedido #{self.pedido_id}: {self.items}")
            monitor.registrar_actividad_agente("mesero", "Agrego items a un pedido abierto",
                                                f"#{self.pedido_id}: {self.items}")

            msg = Message(to="cocinero@127.0.0.1")
            msg.set_metadata("performative", "inform")
            msg.set_metadata("accion", "pedido_actualizado")
            msg.body = db.to_json({"pedido_id": self.pedido_id, "items_agregados": self.items})
            await self.send(msg)

    class CancelarPedidoBehav(OneShotBehaviour):
        """Cancela un pedido (solo si todavia no fue pagado) y avisa a Cocina."""
        def __init__(self, pedido_id):
            super().__init__()
            self.pedido_id = pedido_id

        async def run(self):
            self.cancelado = await db.cancelar_pedido(self.pedido_id)
            if self.cancelado:
                print(f"[Mesero] Pedido #{self.pedido_id} cancelado (clic/chat real).")
                monitor.registrar_actividad_agente("mesero", "Cancelo un pedido", f"#{self.pedido_id}")
                msg = Message(to="cocinero@127.0.0.1")
                msg.set_metadata("performative", "inform")
                msg.set_metadata("accion", "pedido_cancelado")
                msg.body = db.to_json({"pedido_id": self.pedido_id})
                await self.send(msg)
            else:
                print(f"[Mesero] No se pudo cancelar el pedido #{self.pedido_id} (ya pagado o no existe).")

    class MarcarUrgenteBehav(OneShotBehaviour):
        """El cliente mostro estres ALTO (ej: 'urgente apurate'). El
        Mesero manda una senal por XMPP al canal de Cocina para que
        apure ese pedido."""
        def __init__(self, pedido_id, mensaje=""):
            super().__init__()
            self.pedido_id = pedido_id
            self.mensaje = mensaje

        async def run(self):
            msg = Message(to="cocinero@127.0.0.1")
            msg.set_metadata("performative", "request")
            msg.set_metadata("accion", "apurar_pedido")
            msg.body = db.to_json({"pedido_id": self.pedido_id, "mensaje": self.mensaje})
            await self.send(msg)
            print(f"[Mesero] Senal de URGENCIA enviada a Cocina para el pedido #{self.pedido_id}")
            monitor.registrar_actividad_agente("mesero", "Envio senal de URGENCIA a Cocina",
                                                f"#{self.pedido_id}: {self.mensaje}")

    async def setup(self):
        print(f"[Mesero] Agente listo ({self.jid})")


# ==============================================================
# COCINERO: la cola de pedidos vive en restaurante.db (estado).
# El agente solo registra notificaciones y ejecuta las transiciones
# de estado cuando el panel de Cocina se lo pide.
# ==============================================================
class CocineroAgent(Agent):
    class EscucharNotificacionesBehav(CyclicBehaviour):
        """Solo deja constancia (log + XMPP) de que llego un pedido nuevo. No actua solo."""
        async def run(self):
            msg = await self.receive(timeout=15)
            if not msg:
                return
            data = db.from_json(msg.body)
            print(f"[Cocinero] Aviso: pedido #{data['pedido_id']} esperando en cola de cocina.")
            monitor.registrar_actividad_agente("cocinero", "Recibio un pedido en la cola", f"#{data['pedido_id']}")

    class EscucharActualizacionesBehav(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=15)
            if not msg:
                return
            data = db.from_json(msg.body)
            print(
                f"[Cocinero] Aviso: se agregaron items al pedido #{data['pedido_id']} "
                f"ya en cola/preparacion: {data['items_agregados']}"
            )
            monitor.registrar_actividad_agente("cocinero", "Aviso: items agregados a un pedido",
                                                f"#{data['pedido_id']}: {data['items_agregados']}")

    class IniciarPreparacionBehav(OneShotBehaviour):
        def __init__(self, pedido_id):
            super().__init__()
            self.pedido_id = pedido_id

        async def run(self):
            await db.marcar_estado(self.pedido_id, "en_preparacion")
            print(f"[Cocinero] Pedido #{self.pedido_id} -> en_preparacion (clic real del cocinero)")
            monitor.registrar_actividad_agente("cocinero", "Inicio la preparacion", f"#{self.pedido_id}")

    class CompletarPedidoBehav(OneShotBehaviour):
        def __init__(self, pedido_id):
            super().__init__()
            self.pedido_id = pedido_id

        async def run(self):
            await db.marcar_estado(self.pedido_id, "listo")  # dispara el trigger SQL de descuento de stock
            print(f"[Cocinero] Pedido #{self.pedido_id} -> listo (stock descontado, clic real del cocinero)")
            monitor.registrar_actividad_agente("cocinero", "Completo el pedido (descuento stock)",
                                                f"#{self.pedido_id} -> aviso a Caja y Almacen")
            PEDIDOS_URGENTES.discard(self.pedido_id)  # ya no aplica la urgencia

            msg_cajero = Message(to="cajero@127.0.0.1")
            msg_cajero.set_metadata("performative", "inform")
            msg_cajero.set_metadata("accion", "pedido_listo")
            msg_cajero.body = db.to_json({"pedido_id": self.pedido_id})
            await self.send(msg_cajero)

            msg_almacen = Message(to="almacenero@127.0.0.1")
            msg_almacen.set_metadata("performative", "inform")
            msg_almacen.set_metadata("accion", "verificar_stock")
            msg_almacen.body = db.to_json({"origen_pedido_id": self.pedido_id})
            await self.send(msg_almacen)

    class EscucharCancelacionesBehav(CyclicBehaviour):
        """Se entera cuando el Mesero cancela un pedido desde el chat."""
        async def run(self):
            msg = await self.receive(timeout=15)
            if not msg:
                return
            data = db.from_json(msg.body)
            print(f"[Cocinero] Aviso: pedido #{data['pedido_id']} fue cancelado desde el chat.")
            monitor.registrar_actividad_agente("cocinero", "Aviso: un pedido fue cancelado", f"#{data['pedido_id']}")
            PEDIDOS_URGENTES.discard(data["pedido_id"])

    class CancelarPedidoBehav(OneShotBehaviour):
        """Disparado por el boton 'Cancelar' del panel de Cocina."""
        def __init__(self, pedido_id):
            super().__init__()
            self.pedido_id = pedido_id

        async def run(self):
            self.cancelado = await db.cancelar_pedido(self.pedido_id)
            if self.cancelado:
                print(f"[Cocinero] Pedido #{self.pedido_id} cancelado desde el panel de cocina.")
                monitor.registrar_actividad_agente("cocinero", "Cancelo un pedido (panel cocina)", f"#{self.pedido_id}")
                PEDIDOS_URGENTES.discard(self.pedido_id)
            else:
                print(f"[Cocinero] No se pudo cancelar el pedido #{self.pedido_id} (ya pagado o no existe).")

    class EscucharUrgentesBehav(CyclicBehaviour):
        """Recibe la senal de URGENCIA del Mesero (cliente estresado) y
        marca el pedido como urgente para que el cocinero lo priorice."""
        async def run(self):
            msg = await self.receive(timeout=15)
            if not msg:
                return
            data = db.from_json(msg.body)
            pid = data["pedido_id"]
            PEDIDOS_URGENTES.add(pid)
            print(f"[Cocinero] URGENTE: el cliente pide apurar el pedido #{pid}. ({data.get('mensaje','')})")
            monitor.registrar_actividad_agente("cocinero", "Recibio senal de URGENCIA (apurar)",
                                                f"#{pid}: {data.get('mensaje','')}")
            monitor.registrar_evento("warning", f"Cliente pide apurar el pedido #{pid} (estres alto)", origen="cocina")

    async def setup(self):
        print(f"[Cocinero] Agente listo ({self.jid})")
        t1 = Template()
        t1.set_metadata("accion", "preparar_pedido")
        self.add_behaviour(self.EscucharNotificacionesBehav(), t1)

        t2 = Template()
        t2.set_metadata("accion", "pedido_actualizado")
        self.add_behaviour(self.EscucharActualizacionesBehav(), t2)

        t3 = Template()
        t3.set_metadata("accion", "pedido_cancelado")
        self.add_behaviour(self.EscucharCancelacionesBehav(), t3)

        t4 = Template()
        t4.set_metadata("accion", "apurar_pedido")
        self.add_behaviour(self.EscucharUrgentesBehav(), t4)


# ==============================================================
# ALMACENERO: revisa stock cuando el panel se lo pide (boton "Revisar
# ahora"), y tambien queda enterado cuando Cocina completa un pedido.
# ==============================================================
class AlmaceneroAgent(Agent):
    avisos_pendientes = 0  # contador simple de avisos sin revisar, visible al panel

    class EscucharAvisosBehav(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=15)
            if not msg:
                return
            data = db.from_json(msg.body)
            self.agent.avisos_pendientes += 1
            print(
                f"[Almacenero] Aviso: pedido #{data['origen_pedido_id']} completado en cocina, "
                f"hay {self.agent.avisos_pendientes} aviso(s) pendiente(s) de revisar."
            )
            monitor.registrar_actividad_agente("almacenero", "Recibio aviso de pedido completado",
                                                f"#{data['origen_pedido_id']} ({self.agent.avisos_pendientes} sin revisar)")

    class RevisarStockBehav(OneShotBehaviour):
        """Disparado por el boton 'Revisar ahora' del panel de Almacen."""
        async def run(self):
            print("[Almacenero] Revisando stock (clic real del almacenero)...")
            bajos = await db.insumos_bajo_stock()
            por_vencer = await db.insumos_por_vencer(dias=3)

            for i in bajos:
                texto = (
                    f"Stock bajo: {i['nombre']} tiene {i['stock_actual']} {i['unidad_medida']}, "
                    f"por debajo del minimo de {i['stock_minimo']} {i['unidad_medida']}."
                )
                self._registrar_en_historial(texto, "alerta_stock_bajo", i["nombre"])

            for i in por_vencer:
                texto = f"El insumo {i['nombre']} vence el {i['fecha_vencimiento']}, dentro de los proximos 3 dias."
                self._registrar_en_historial(texto, "alerta_vencimiento", i["nombre"])

            self.agent.avisos_pendientes = 0
            self.resultado = {"bajos": bajos, "por_vencer": por_vencer}
            print(f"[Almacenero] Revision completada: {len(bajos)} bajo stock, {len(por_vencer)} por vencer.")
            monitor.registrar_actividad_agente("almacenero", "Reviso el stock",
                                                f"{len(bajos)} bajo minimo, {len(por_vencer)} por vencer")

        def _registrar_en_historial(self, texto, tipo, insumo):
            try:
                chroma_db.registrar_evento(texto, tipo, insumo)
            except Exception as e:
                print(f"[Almacenero] Aviso: no se pudo registrar en el historial (ChromaDB/Ollama): {e}")

    async def setup(self):
        print(f"[Almacenero] Agente listo ({self.jid})")
        t = Template()
        t.set_metadata("accion", "verificar_stock")
        self.add_behaviour(self.EscucharAvisosBehav(), t)


# ==============================================================
# CAJERO: cobra cuando el panel de Caja se lo pide (pedido en estado 'listo')
# ==============================================================
class CajeroAgent(Agent):
    class EscucharPedidosListosBehav(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=15)
            if not msg:
                return
            data = db.from_json(msg.body)
            print(f"[Cajero] Aviso: pedido #{data['pedido_id']} listo, esperando cobro en caja.")
            monitor.registrar_actividad_agente("cajero", "Recibio un pedido listo para cobro", f"#{data['pedido_id']}")

    class CobrarPedidoBehav(OneShotBehaviour):
        def __init__(self, pedido_id):
            super().__init__()
            self.pedido_id = pedido_id

        async def run(self):
            pedido = await db.obtener_pedido(self.pedido_id)
            await db.marcar_estado(self.pedido_id, "pagado")
            self.total = pedido["total"]
            print(f"[Cajero] Pedido #{self.pedido_id} cobrado y pagado (clic real del cajero). Total: S/ {self.total:.2f}")
            monitor.registrar_actividad_agente("cajero", "Cobro un pedido", f"#{self.pedido_id}: S/ {self.total:.2f}")

    async def setup(self):
        print(f"[Cajero] Agente listo ({self.jid})")
        t = Template()
        t.set_metadata("accion", "pedido_listo")
        self.add_behaviour(self.EscucharPedidosListosBehav(), t)