"""Reproduce the project's original, dependency-free notification tones."""
import math
from pathlib import Path
import struct
import wave

ROOT = Path(__file__).resolve().parents[1] / "face_attendance" / "assets" / "sounds"
# Frequency (Hz), seconds. Zero is silence. Short fades avoid clicks.
TONES = {
    "detected": [(660, .09), (0, .035), (880, .10)],
    "lost": [(440, .13), (0, .04), (330, .16)],
    "unknown": [(392, .13), (0, .07), (392, .13), (0, .07), (294, .18)],
    "guidance": [(520, .12), (0, .08), (520, .12)],
    "verified": [(784, .10), (0, .04), (988, .15)],
    "success": [(659, .12), (0, .025), (831, .12), (0, .025), (988, .23)],
    "error": [(330, .19), (0, .06), (247, .25)],
    "disconnected": [(587, .13), (0, .05), (440, .13), (0, .05), (294, .24)],
}


def generate():
    ROOT.mkdir(parents=True, exist_ok=True)
    for name, notes in TONES.items():
        samples = bytearray()
        for frequency, duration in notes:
            count = round(22050 * duration)
            for i in range(count):
                t = i / 22050
                fade = min(1., i / 220., (count - 1 - i) / 440.)
                value = .22 * fade * math.sin(2 * math.pi * frequency * t)
                samples.extend(struct.pack('<h', round(value * 32767)))
        with wave.open(str(ROOT / (name + '.wav')), 'wb') as output:
            output.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
            output.writeframes(samples)


if __name__ == '__main__':
    generate()
