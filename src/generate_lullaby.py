#!/usr/bin/env python3
"""Create an original, quiet instrumental test lullaby (not a known melody)."""
from pathlib import Path
import math
import struct
import wave

RATE = 22050
DURATION = 24
DEST = Path(__file__).resolve().parent.parent / "audio" / "lullaby.wav"
# 4 harmonies, soft 1-second plucks, and no heavy bass.
chords = [
    (261.63, 329.63, 392.00, 523.25),
    (220.00, 261.63, 349.23, 440.00),
    (196.00, 246.94, 329.63, 392.00),
    (174.61, 220.00, 293.66, 349.23),
]


def pluck(phase, frequency, local_time):
    attack = min(1.0, local_time / 0.07)
    tail = math.exp(-2.2 * local_time)
    v = (math.sin(phase) + 0.17 * math.sin(2 * phase) + 0.06 * math.sin(3 * phase))
    return attack * tail * v


with wave.open(str(DEST), 'wb') as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(RATE)
    buff = bytearray()
    for index in range(RATE * DURATION):
        t = index / RATE
        chord = chords[int(t // 6) % 4]
        tick = int(t * 1.5)
        local = t - tick / 1.5
        freq = chord[tick % 4]
        main = pluck(2 * math.pi * freq * t, freq, local)
        underneath = 0.22 * math.sin(2 * math.pi * chord[0] * t)
        fade_in = min(1.0, t / 1.0)
        fade_out = min(1.0, (DURATION - t) / 1.0)
        output = (main * 0.12 + underneath * 0.08) * fade_in * fade_out
        clipped = max(-1.0, min(1.0, output))
        buff.extend(struct.pack('<h', int(clipped * 32767)))
    wf.writeframes(buff)
print(f"Created {DEST}: {DURATION}s, 22.05 kHz mono 16-bit PCM")
