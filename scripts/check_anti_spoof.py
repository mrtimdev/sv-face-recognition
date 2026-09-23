"""Offline PAD diagnostic; no identities, attendance database or notifications."""
import argparse
import json
from pathlib import Path
import sys

# Support running directly from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
from face_attendance.face_backend import OpenCVFaceBackend

from face_attendance.anti_spoof import AntiSpoofService, LIVE_THRESHOLD


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Local image or camera-recorded video')
    parser.add_argument('--interval', type=float, default=.5, help='Seconds between samples')
    parser.add_argument('--max-samples', type=int, default=60)
    args = parser.parse_args()
    if not args.source.is_file() or args.interval <= 0 or args.max_samples <= 0:
        parser.error('Provide an existing local file and positive sampling limits')
    cv2.setNumThreads(1)
    model = AntiSpoofService()
    detector = OpenCVFaceBackend()
    if model.error:
        parser.exit(1, model.error + '\n')
    cap = cv2.VideoCapture(str(args.source))
    if not cap.isOpened():
        parser.exit(1, 'Cannot decode the local recording\n')
    results = []
    try:
        for index in range(args.max_samples):
            at = index * args.interval
            cap.set(cv2.CAP_PROP_POS_MSEC, at * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            ratio = min(1., 720 / max(frame.shape[:2]))
            if ratio < 1:
                frame = cv2.resize(frame, None, fx=ratio, fy=ratio)
            boxes = detector.face_locations(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            faces = []
            for box in boxes:
                score, error = model.evaluate(frame, box)
                faces.append({'box_trbl': box, 'live_score': score,
                              'sample_pass': score is not None and score >= LIVE_THRESHOLD,
                              'error': error})
            results.append({'seconds': at, 'faces': faces})
    finally:
        cap.release()
    print(json.dumps({'threshold': LIVE_THRESHOLD, 'frames': results,
                      'note': 'PAD sample results only; no attendance recorded. Source selfie video is not a screen-replay test.'}, indent=2))


if __name__ == '__main__':
    main()
