"""Offline 1/3/5-face latency comparison; never claims live camera accuracy.

Run with: python scripts/benchmark_detection.py --image path/to/portrait.jpg
Uses repeated copies of one portrait to measure load, not identity separation.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from face_attendance.config import Config
from face_attendance.models import FramePacket, RecognitionRequest
from face_attendance.recognition import RecognitionService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--iterations', type=int, default=12)
    args = parser.parse_args()
    portrait = cv2.imread(args.image)
    if portrait is None or args.iterations < 1:
        parser.error('Provide a readable portrait image and a positive iteration count')
    cv2.setNumThreads(1)
    # Fit the entire portrait into a 240x600 tile without changing its aspect ratio.
    factor = min(240 / portrait.shape[1], 600 / portrait.shape[0])
    tile = cv2.resize(portrait, None, fx=factor, fy=factor)
    catalog = SimpleNamespace(employees=(), encodings=np.empty((0, 128)))
    output = []
    for count in (1, 3, 5):
        frame = np.full((720, 1280, 3), 100, np.uint8)
        for index in range(count):
            x = 40 + index * 240
            frame[100:100 + tile.shape[0], x:x + tile.shape[1]] = tile
        for scale in (.25, .5):
            service = RecognitionService(replace(Config(), detection_scale=scale), catalog)
            elapsed, detected, suitable = [], [], []
            for i in range(args.iterations + 2):
                packet = FramePacket(i, time.monotonic(), time.time(), 1, frame)
                result = service.process(RecognitionRequest(packet, ()))
                if i >= 2:
                    elapsed.append(result.elapsed * 1000)
                    detected.append(len(result.detections))
                    suitable.append(sum(d.quality_ok for d in result.detections))
            output.append(dict(people=count, scale=scale, detected=min(detected),
                               quality_pass=min(suitable), median_ms=round(float(np.median(elapsed)), 1),
                               p95_ms=round(float(np.percentile(elapsed, 95)), 1)))
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
