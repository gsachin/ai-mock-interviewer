"""Audio format normalization for the voice pipeline.

Format contract (see ``protocols.TTSEngine``): every TTS engine emits
**48 kHz mono s16le PCM** — the LiveKit room format — so playback sinks are
format-agnostic. Providers return their native formats (Cartesia raw f32le
24 kHz, ElevenLabs mp3, Piper/Kokoro wav); this module converts each to the
contract. numpy is the only dependency (already in the ``voice`` extra).
"""
import io
import wave

import numpy as np

LIVEKIT_SAMPLE_RATE = 48000


def _resample_linear(samples: np.ndarray, src_rate: int) -> np.ndarray:
    """Linear-interpolation resample to 48 kHz (mono, any dtype)."""
    if src_rate == LIVEKIT_SAMPLE_RATE:
        return samples
    n = max(1, round(len(samples) * LIVEKIT_SAMPLE_RATE / src_rate))
    src_x = np.arange(len(samples), dtype=np.float64)
    dst_x = np.linspace(0.0, float(len(samples) - 1), num=n)
    return np.interp(dst_x, src_x, samples)


def f32le_to_s16le48k(audio: bytes, sample_rate: int) -> bytes:
    """Cartesia raw PCM: float32 little-endian mono -> 48 kHz s16le."""
    samples = np.frombuffer(audio, dtype="<f4").astype(np.float64)
    samples = np.clip(samples, -1.0, 1.0)
    samples = _resample_linear(samples, sample_rate)
    return (samples * 32767.0).astype("<i2").tobytes()


def s16le_to_s16le48k(audio: bytes, sample_rate: int,
                      num_channels: int = 1) -> bytes:
    """Interleaved s16le (any rate, possibly multi-channel) -> 48 kHz mono
    s16le. Extra channels are dropped (channel 0 kept)."""
    samples = np.frombuffer(audio, dtype="<i2")
    if num_channels > 1:
        samples = samples.reshape(-1, num_channels)[:, 0]
    samples = _resample_linear(samples.astype(np.float64), sample_rate)
    return np.clip(samples, -32768.0, 32767.0).astype("<i2").tobytes()


def wav_to_s16le48k(audio: bytes) -> bytes:
    """Self-describing RIFF wav (Piper/Kokoro emit this) -> 48 kHz mono
    s16le. Handles 8/16/24/32-bit PCM via the stdlib ``wave`` module."""
    with wave.open(io.BytesIO(audio), "rb") as wav:
        n_channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    dtype = {1: "u1", 2: "<i2", 4: "<i4"}.get(width)
    if dtype is None:  # 3-byte PCM (rare) — pad to 4 bytes
        raw = b"".join(b"\x00" + raw[i:i + 3] for i in range(0, len(raw), 3))
        dtype = "<i4"
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float64)
    if width == 1:  # unsigned 8-bit centered on 128
        samples -= 128.0
        samples *= 256.0
    if width == 4:
        samples /= 256.0  # 24-bit in 4-byte containers -> 16-bit scale
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels)[:, 0]
    samples = _resample_linear(samples, sample_rate)
    return np.clip(samples, -32768.0, 32767.0).astype("<i2").tobytes()


def mp3_to_s16le48k(audio: bytes) -> bytes:
    """MP3 (ElevenLabs output) -> 48 kHz mono s16le. pydub+ffmpeg is a heavy
    decode chain, so it stays an optional import — only ElevenLabs needs it."""
    try:
        from pydub import AudioSegment
    except ImportError as exc:
        raise ImportError(
            "mp3 decoding needs pydub+ffmpeg — pip install pydub (or use the "
            "cartesia/kokoro/piper TTS providers, which emit PCM/wav)"
        ) from exc
    segment = AudioSegment.from_mp3(io.BytesIO(audio)).set_channels(1)
    return s16le_to_s16le48k(segment.raw_data, segment.frame_rate)
