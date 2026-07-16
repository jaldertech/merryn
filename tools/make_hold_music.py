"""Generates Merryn's hold music: a licence-free muzak loop.

Synthesised from scratch (no samples, no third-party audio) so there is
nothing to licence. Eight bars of a soft electric-piano arpeggio over a
Cmaj7 / Am7 / Fmaj7 / G7 progression with a sine bass, written as a
mono 16-bit 48 kHz WAV. Playback loops it seamlessly via ffmpeg's
-stream_loop, so ~22 seconds of material is all that is needed —
repetition is rather the point of hold music.

Usage: python make_hold_music.py <output.wav>
"""
from __future__ import annotations

import array
import math
import sys
import wave
from pathlib import Path

RATE = 48000
BPM = 88
BEAT = 60.0 / BPM
BAR = 4 * BEAT

# (bass MIDI note, arpeggio chord MIDI notes), one entry per bar.
PROGRESSION = [
    (48, [60, 64, 67, 71]),  # Cmaj7
    (45, [57, 60, 64, 67]),  # Am7
    (41, [53, 57, 60, 64]),  # Fmaj7
    (43, [55, 59, 62, 65]),  # G7
] * 2

# Eighth-note arpeggio pattern: indices into the chord, rising then
# turning back — the canonical noodling of on-hold telephony.
ARP_PATTERN = [0, 1, 2, 3, 2, 3, 1, 2]


def freq(midi_note: int) -> float:
    return 440.0 * 2 ** ((midi_note - 69) / 12)


def add_note(
    buf: list[float],
    start: float,
    duration: float,
    hz: float,
    volume: float,
    harmonics: tuple[float, ...],
) -> None:
    """Mixes one enveloped tone into the buffer at `start` seconds."""
    first = int(start * RATE)
    count = int(duration * RATE)
    attack = int(0.012 * RATE)
    for i in range(count):
        idx = first + i
        if idx >= len(buf):
            break
        t = i / RATE
        env = (i / attack) if i < attack else math.exp(-2.6 * t / duration)
        sample = 0.0
        for order, weight in enumerate(harmonics, start=1):
            sample += weight * math.sin(2 * math.pi * hz * order * t)
        buf[idx] += volume * env * sample


def main() -> None:
    out = Path(sys.argv[1])
    total_seconds = len(PROGRESSION) * BAR
    buf = [0.0] * int(total_seconds * RATE)

    for bar_index, (bass_note, chord) in enumerate(PROGRESSION):
        bar_start = bar_index * BAR
        # Bass: root held for the bar, mellow and quiet.
        add_note(
            buf,
            bar_start,
            BAR * 0.98,
            freq(bass_note),
            volume=0.30,
            harmonics=(1.0, 0.20),
        )
        # Arpeggio: eighth notes, slightly detached, e-piano-ish tone.
        for step, chord_index in enumerate(ARP_PATTERN):
            add_note(
                buf,
                bar_start + step * (BEAT / 2),
                (BEAT / 2) * 0.92,
                freq(chord[chord_index]),
                volume=0.22,
                harmonics=(1.0, 0.35, 0.12),
            )

    peak = max(abs(s) for s in buf)
    scale = 0.45 / peak if peak else 0.0
    pcm = array.array("h", (int(s * scale * 32767) for s in buf))

    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm.tobytes())
    print(f"Wrote {out} ({total_seconds:.1f}s, {out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
