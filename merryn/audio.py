"""Voice playback without ffmpeg.

The bundled hold music is a plain 48 kHz 16-bit WAV, so playback needs
no external decoder: the file is read once with the standard-library
wave module and served to discord.py as raw PCM frames, looping
seamlessly. Opus encoding is done by discord.py via libopus, which
this module locates — an explicit override, a copy shipped inside a
frozen build, or the system library, in that order.
"""
from __future__ import annotations

import array
import ctypes.util
import logging
import os
import sys
import wave
from pathlib import Path

import discord

log = logging.getLogger("merryn")

SAMPLE_RATE = 48000
FRAME_SAMPLES = 960  # 20 ms at 48 kHz, what discord.py reads per tick
FRAME_BYTES = FRAME_SAMPLES * 2 * 2  # stereo, 16-bit


def bundle_dir() -> Path | None:
    """Root of the frozen-build bundle when running one, else None."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def resolve_hold_music() -> Path:
    """HOLD_MUSIC_FILE override, else the WAV shipped with the package."""
    override = os.environ.get("HOLD_MUSIC_FILE")
    if override:
        return Path(override)
    root = bundle_dir()
    if root is not None:
        return root / "merryn" / "assets" / "hold_music.wav"
    return Path(__file__).parent / "assets" / "hold_music.wav"


def ensure_opus() -> bool:
    """Loads libopus for voice encoding. Returns True when available."""
    if discord.opus.is_loaded():
        return True
    try:
        discord.opus._load_default()
    except Exception:
        pass
    if discord.opus.is_loaded():
        return True

    candidates: list[str] = []
    override = os.environ.get("OPUS_LIBRARY")
    if override:
        candidates.append(override)
    root = bundle_dir()
    if root is not None:
        candidates.extend(str(p) for p in root.glob("libopus*"))
    found = ctypes.util.find_library("opus")
    if found:
        candidates.append(found)
    candidates.extend(
        [
            "/usr/lib/x86_64-linux-gnu/libopus.so.0",
            "/usr/lib/aarch64-linux-gnu/libopus.so.0",
            "/opt/homebrew/lib/libopus.0.dylib",
            "/usr/local/lib/libopus.0.dylib",
        ]
    )
    for candidate in candidates:
        try:
            discord.opus.load_opus(candidate)
            log.info("Loaded libopus from %s", candidate)
            return True
        except OSError:
            continue
    return discord.opus.is_loaded()


class LoopingWAVAudio(discord.AudioSource):
    """Loops a 48 kHz 16-bit PCM WAV (mono or stereo) until stopped.

    The whole file is decoded into memory up front (hold music is a few
    megabytes at most), so read() is allocation-light and the loop
    wraps mid-frame with no gap.
    """

    def __init__(self, path: Path) -> None:
        with wave.open(str(path), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getframerate() != SAMPLE_RATE:
                raise ValueError(
                    f"{path.name}: hold music must be a 16-bit {SAMPLE_RATE} Hz WAV "
                    f"(got {wav.getsampwidth() * 8}-bit {wav.getframerate()} Hz)"
                )
            channels = wav.getnchannels()
            if channels not in (1, 2):
                raise ValueError(f"{path.name}: expected mono or stereo")
            raw = wav.readframes(wav.getnframes())
        if channels == 1:
            # Interleave each sample into both channels. Pure 2-byte
            # copies — sample values are never interpreted, so this is
            # byte-order agnostic.
            mono = array.array("h")
            mono.frombytes(raw)
            stereo = array.array("h", bytes(len(raw) * 2))
            stereo[0::2] = mono
            stereo[1::2] = mono
            raw = stereo.tobytes()
        if not raw:
            raise ValueError(f"{path.name}: no audio frames")
        self._pcm = raw
        self._pos = 0

    def read(self) -> bytes:
        out = bytearray()
        while len(out) < FRAME_BYTES:
            chunk = self._pcm[self._pos : self._pos + FRAME_BYTES - len(out)]
            out += chunk
            self._pos = (self._pos + len(chunk)) % len(self._pcm)
        return bytes(out)

    def is_opus(self) -> bool:
        return False
