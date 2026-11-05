"""
generate_alert_sound.py

Generates a simple alert beep (alert.wav) so you don't need to source
an external audio file. Run this once before recognize.py.

Usage:
    python generate_alert_sound.py
"""

import wave
import struct
import math

OUTPUT_PATH = "alert.wav"
FRAMERATE = 44100
DURATION_SEC = 0.6
FREQUENCY_HZ = 1000  # tone pitch
VOLUME = 0.5  # 0.0 - 1.0


def generate_beep():
    n_samples = int(FRAMERATE * DURATION_SEC)
    with wave.open(OUTPUT_PATH, "w") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(FRAMERATE)

        for i in range(n_samples):
            t = i / FRAMERATE
            # simple sine tone with a slight fade-out to avoid clicking
            fade = 1.0 - (i / n_samples) * 0.3
            sample = VOLUME * fade * math.sin(2 * math.pi * FREQUENCY_HZ * t)
            packed = struct.pack("<h", int(sample * 32767))
            wav_file.writeframesraw(packed)

    print(f"Generated {OUTPUT_PATH}")


if __name__ == "__main__":
    generate_beep()
