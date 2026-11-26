"""
voz.py
------
PEDIDOS POR VOZ para el mesero: transcripcion LOCAL con Whisper
(faster-whisper). El audio nunca sale de la maquina, igual que el resto del
sistema (IA local y datos privados).

  - Modelo configurable con WHISPER_MODELO en .env (por defecto 'small':
    buen equilibrio entre precision y velocidad en espanol, en CPU).
    Para maxima velocidad en PCs modestas: WHISPER_MODELO=base
  - Se le pasa la CARTA como contexto (initial_prompt) para que reconozca
    bien los nombres de los platos (Donoso, salchipapa, tequenos...).
  - Indica si la transcripcion es confiable; si es dudosa, el panel la deja
    para que el mesero la revise antes de enviarla.
  - Si faster-whisper no esta instalado, el sistema funciona igual y el
    boton de microfono explica como activarlo.

OPTIMIZACION DE LATENCIA (los pedidos son frases cortas):
  - Decodificacion voraz (beam_size=1) y temperatura fija 0: sin las
    re-decodificaciones de respaldo que Whisper hace cuando duda, que en
    audio de restaurante (ruido) podian multiplicar el tiempo.
  - Sin marcas de tiempo (no se necesitan para un pedido).
  - Usa los nucleos fisicos de la CPU. Con GPU NVIDIA (CUDA + cuDNN
    instalados) se puede activar con WHISPER_DISPOSITIVO=cuda en .env; no es
    automatico porque en Windows, si faltan las DLL de cuDNN, el proceso se
    cierra sin aviso.
  - Precalentamiento al arrancar: la primera transcripcion real ya no paga
    el costo de inicializacion del modelo.

Instalacion (una vez, dentro de env_tesis):
    pip install faster-whisper
La primera vez descarga el modelo (~460 MB para 'small') y queda en cache.
"""
import io
import os
import threading
import time

try:
    from faster_whisper import WhisperModel
    DISPONIBLE = True
except ImportError:
    WhisperModel = None
    DISPONIBLE = False

MAX_AUDIO_BYTES = 8 * 1024 * 1024     # ~1 minuto de audio comprimido es mucho menos
UMBRAL_LOGPROB = -1.0                 # mismo umbral que usa Whisper para descartar
UMBRAL_SIN_VOZ = 0.6

_modelo = None
_dispositivo = None
_error = None
_cargando = False
_lock = threading.Lock()


def _nombre_modelo() -> str:
    # Se lee al cargar (no al importar) para respetar lo que haya en .env
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    except ImportError:
        pass
    return (os.environ.get("WHISPER_MODELO") or "small").strip()


MODELO = _nombre_modelo()


def _hilos_cpu() -> int:
    logicos = os.cpu_count() or 4
    # CTranslate2 rinde mejor con nucleos fisicos (los logicos suelen ser el doble)
    return max(2, logicos // 2 if logicos >= 8 else logicos)


def estado() -> dict:
    return {"disponible": DISPONIBLE, "cargado": _modelo is not None, "cargando": _cargando,
            "modelo": MODELO, "dispositivo": _dispositivo, "error": _error,
            "instalar": None if DISPONIBLE else "pip install faster-whisper"}


def _precalentar(modelo):
    """Una pasada corta sobre audio sintetico para inicializar el motor."""
    import numpy as np
    sr = 16000
    t = np.arange(int(sr * 1.5)) / sr
    ruido = (0.05 * np.sin(2 * np.pi * 220 * t) + 0.01 * np.random.randn(t.size)).astype(np.float32)
    segs, _ = modelo.transcribe(ruido, language="es", beam_size=1, temperature=0.0,
                                without_timestamps=True, vad_filter=False)
    list(segs)


def _cargar():
    global _modelo, _dispositivo, _error, _cargando, MODELO
    with _lock:
        if _modelo is not None:
            return _modelo
        _cargando = True
        MODELO = _nombre_modelo()
        try:
            opciones = []
            if os.environ.get("WHISPER_DISPOSITIVO", "").strip().lower() == "cuda":
                opciones.append(("cuda", "float16"))
            opciones.append(("cpu", "int8"))
            ultimo_error = None
            for dispositivo, tipo in opciones:
                try:
                    t0 = time.perf_counter()
                    kwargs = {"cpu_threads": _hilos_cpu()} if dispositivo == "cpu" else {}
                    modelo = WhisperModel(MODELO, device=dispositivo, compute_type=tipo, **kwargs)
                    _precalentar(modelo)
                    _modelo, _dispositivo, _error = modelo, dispositivo, None
                    detalle = f"{kwargs['cpu_threads']} hilos" if kwargs else "GPU"
                    print(f"[voz] Whisper '{MODELO}' listo en {time.perf_counter() - t0:.1f}s "
                          f"(local, {dispositivo.upper()}, {detalle}).")
                    return _modelo
                except Exception as e:
                    ultimo_error = e
                    print(f"[voz] No se pudo usar {dispositivo.upper()} ({e}); probando la siguiente opcion...")
            _error = str(ultimo_error)
            raise ultimo_error
        finally:
            _cargando = False


def precargar():
    """Carga y precalienta el modelo en segundo plano al arrancar
    (la 1ra vez lo descarga)."""
    if not DISPONIBLE:
        print("[voz] Pedidos por voz desactivados: instala con 'pip install faster-whisper'.")
        return

    def _tarea():
        try:
            _cargar()
        except Exception as e:
            print(f"[voz] Aviso: no se pudo cargar Whisper '{MODELO}': {e}")
    threading.Thread(target=_tarea, daemon=True, name="whisper-precarga").start()


def transcribir(audio: bytes, contexto: str = "") -> dict:
    """Transcribe audio (webm/ogg/mp4/wav) en espanol. Bloqueante: usar en un hilo."""
    if not DISPONIBLE:
        raise RuntimeError("faster-whisper no esta instalado (pip install faster-whisper)")
    if not audio:
        raise ValueError("audio vacio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("audio demasiado largo")
    t_espera = time.perf_counter()
    modelo = _cargar()
    espera_ms = int((time.perf_counter() - t_espera) * 1000)
    t0 = time.perf_counter()
    segmentos, info = modelo.transcribe(
        io.BytesIO(audio), language="es", task="transcribe",
        initial_prompt=contexto or None,
        beam_size=1, best_of=1, temperature=0.0,          # rapido: sin re-decodificar
        without_timestamps=True, condition_on_previous_text=False,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200},
    )
    segmentos = list(segmentos)
    ms = int((time.perf_counter() - t0) * 1000)
    texto = " ".join(s.text.strip() for s in segmentos).strip()
    if segmentos:
        logprob = sum(s.avg_logprob for s in segmentos) / len(segmentos)
        sin_voz = max(s.no_speech_prob for s in segmentos)
    else:
        logprob, sin_voz = -10.0, 1.0
    seg_audio = round(getattr(info, "duration", 0.0), 1)
    print(f"[voz] {seg_audio}s de audio transcritos en {ms / 1000:.2f}s "
          f"({MODELO}, {_dispositivo}){f' + {espera_ms / 1000:.1f}s esperando carga' if espera_ms > 200 else ''}: {texto!r}")
    return {
        "texto": texto,
        "confiable": bool(texto) and logprob > UMBRAL_LOGPROB and sin_voz < UMBRAL_SIN_VOZ,
        "segundos_audio": seg_audio,
        "ms": ms,
        "espera_carga_ms": espera_ms,
        "modelo": MODELO,
        "dispositivo": _dispositivo,
    }
