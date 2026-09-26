"""Offline PAD diagnostic; no identities, attendance database or notifications."""
import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
from face_attendance.face_backend import OpenCVFaceBackend
from face_attendance.geometry import association_cost
from face_attendance.anti_spoof import AntiSpoofService, LIVE_THRESHOLD, PresentationGuard, live_sample


def frames_from_file(path, interval, limit):
    """Decode once, preserving original camera pixels and video timestamps."""
    image = cv2.imread(str(path))
    if image is not None:
        yield 0., image
        return
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise ValueError('Cannot decode the local recording')
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('Video has no usable frame rate')
        index, sampled, due = 0, 0, 0.
        while sampled < limit:
            ok, frame = cap.read()
            if not ok:
                break
            at = index / fps
            index += 1
            if at + 1e-9 < due:
                continue
            yield at, frame
            sampled += 1
            due = at + interval
    finally:
        cap.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Local image or camera-recorded attack/live video')
    parser.add_argument('--interval', type=float, default=.1, help='Seconds between samples')
    parser.add_argument('--max-samples', type=int, default=300)
    parser.add_argument('--detection-scale', type=float, default=.25, help='Match the dashboard detection scale')
    parser.add_argument('--presence-sec', type=float, default=3., help='Match the attendance dwell time')
    parser.add_argument('--expected', choices=('live', 'attack'), help='Return nonzero when the expected check fails')
    parser.add_argument('--report', type=Path, help='Optional JSON report destination')
    args = parser.parse_args(argv)
    if (not args.source.is_file() or args.max_samples <= 0
            or not all(math.isfinite(v) and v > 0 for v in (args.interval, args.presence_sec, args.detection_scale))
            or args.detection_scale > 1):
        parser.error('Provide an existing local file and valid positive sampling limits')
    cv2.setNumThreads(1)
    model, detector = AntiSpoofService(), OpenCVFaceBackend()
    if model.error:
        parser.exit(1, model.error + '\n')
    guard = PresentationGuard(min_span=args.presence_sec)
    results, previous_box = [], None
    try:
        for at, frame in frames_from_file(args.source, args.interval, args.max_samples):
            height, width = frame.shape[:2]
            small = cv2.resize(frame, (max(1, round(width * args.detection_scale)),
                                       max(1, round(height * args.detection_scale))))
            boxes = detector.face_locations(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
            sy, sx = height / small.shape[0], width / small.shape[1]
            boxes = [(t * sy, r * sx, b * sy, l * sx) for t, r, b, l in boxes]
            faces = []
            # A diagnostic sequence must contain one continuously associated face.
            if len(boxes) != 1:
                guard.clear()
                previous_box = None
            for box in boxes:
                result = model.inspect(frame, box)
                if len(boxes) == 1:
                    if previous_box is not None and association_cost(box, previous_box) >= .85:
                        guard.clear()
                    guard.update(1, result['live_score'], at)
                    previous_box = box
                faces.append({'box_trbl': box, **result,
                              'sample_pass': live_sample(result['live_score']),
                              'window_pass': len(boxes) == 1 and guard.passed(1, at)})
            results.append({'seconds': at, 'faces': faces})
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    faces = [face for sample in results for face in sample['faces']]
    passing = sum(face['sample_pass'] for face in faces)
    summary = {'frames': len(results), 'faces': len(faces), 'passing_samples': passing,
               'passing_windows': sum(face['window_pass'] for face in faces),
               'errors': sum(bool(face['error']) for face in faces),
               'frames_without_faces': sum(not sample['faces'] for sample in results)}
    # A missed detection / model error is inconclusive, never a successful attack test.
    expected_ok = bool(faces) and summary['errors'] == 0 and summary['frames_without_faces'] == 0
    if args.expected == 'attack':
        expected_ok = expected_ok and passing == 0
    elif args.expected == 'live':
        expected_ok = expected_ok and (passing > 0 if len(results) == 1 else summary['passing_windows'] > 0)
    report = {'threshold': LIVE_THRESHOLD, 'presence_sec': guard.min_span,
              'expected': args.expected, 'expected_check_passed': expected_ok if args.expected else None,
              'summary': summary, 'frames': results,
              'note': 'PAD only; no attendance recorded. A source selfie is not a camera recapture of a print/screen. Scores are not measured accuracy. Multi-face sequences do not authorize a diagnostic window.'}
    output = json.dumps(report, indent=2)
    if args.report:
        args.report.write_text(output + '\n', encoding='utf-8')
    print(output)
    return 0 if args.expected is None or expected_ok else 2


if __name__ == '__main__':
    raise SystemExit(main())
