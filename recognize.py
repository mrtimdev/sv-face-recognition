"""
recognize.py  —  Enhanced UI Edition

Features:
  • Corner-bracket face tracking (L-shaped corners, not plain boxes)
  • Known employee: green brackets + name label + live duration counter
    + a fill-bar showing time in frame (fills over 60 s)
  • Unknown face: pulsing red brackets → "AUTO RESET" flash → fades out
    automatically after UNKNOWN_RESET_SEC with no manual action needed
  • Right-side HUD: FPS, enrolled count, active faces, live clock
  • Animated horizontal scan line across the video feed area

Usage:
    python recognize.py
    python recognize.py --source "rtsp://user:pass@192.168.1.50:554/stream1"
    python recognize.py --tolerance 0.45 --process-every 2
"""

import argparse
import csv
import os
import pickle
import threading
import time
from datetime import datetime

import cv2
import numpy as np
import face_recognition

try:
    import simpleaudio as sa
    HAVE_AUDIO = True
except ImportError:
    HAVE_AUDIO = False

# ── Config ────────────────────────────────────────────────────────────────────
ENCODINGS_PATH      = "encodings.pickle"
ALERT_SOUND_PATH    = "alert.wav"
LOG_PATH            = "attendance_log.csv"

ALERT_COOLDOWN_SEC  = 3.0    # minimum gap between alert sounds
LOG_COOLDOWN_SEC    = 10.0   # avoid re-logging the same person every frame
UNKNOWN_RESET_SEC   = 3.0    # unknown flash stays visible this long then fades
SESSION_EXPIRE_SEC  = 3.0    # clear a known session after N seconds out of frame

# Colours (BGR)
C_GREEN  = (40,  220,  80)
C_RED    = (40,   50, 240)
C_AMBER  = (30,  190, 255)
C_TEAL   = (180, 200,  40)
C_WHITE  = (240, 240, 240)
C_DARK   = ( 18,  18,  18)
C_GRAY   = ( 80,  80,  80)

HUD_W    = 230   # width of the right-side HUD panel in pixels


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_known_faces():
    if not os.path.exists(ENCODINGS_PATH):
        raise FileNotFoundError(
            f"{ENCODINGS_PATH} not found. "
            "Enroll at least one employee first with enroll_faces.py"
        )
    with open(ENCODINGS_PATH, "rb") as f:
        data = pickle.load(f)
    names, encodings = [], []
    for name, enc_list in data.items():
        for enc in enc_list:
            names.append(name)
            encodings.append(enc)
    return names, encodings


def play_alert_async():
    if not HAVE_AUDIO or not os.path.exists(ALERT_SOUND_PATH):
        return
    def _play():
        try:
            sa.WaveObject.from_wave_file(ALERT_SOUND_PATH).play()
        except Exception as exc:
            print(f"Audio error: {exc}")
    threading.Thread(target=_play, daemon=True).start()


def log_event(name: str):
    exists = os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["timestamp", "name", "status"])
        w.writerow([
            datetime.now().isoformat(timespec="seconds"),
            name,
            "KNOWN" if name != "UNKNOWN" else "UNKNOWN",
        ])


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    m = s // 60
    return f"{m}m {s % 60:02d}s" if m else f"{s}s"


# ── Drawing primitives ────────────────────────────────────────────────────────

def draw_corners(img, x1, y1, x2, y2, color, thickness=2, arm=None):
    """L-shaped corner brackets around a face box."""
    if arm is None:
        arm = max(18, (x2 - x1) // 4)
    pts = [
        # top-left
        ((x1, y1), (x1 + arm, y1)), ((x1, y1), (x1, y1 + arm)),
        # top-right
        ((x2, y1), (x2 - arm, y1)), ((x2, y1), (x2, y1 + arm)),
        # bottom-left
        ((x1, y2), (x1 + arm, y2)), ((x1, y2), (x1, y2 - arm)),
        # bottom-right
        ((x2, y2), (x2 - arm, y2)), ((x2, y2), (x2, y2 - arm)),
    ]
    for p1, p2 in pts:
        cv2.line(img, p1, p2, color, thickness, cv2.LINE_AA)


def draw_pill_label(img, text, x, y, bg_color, text_color=C_WHITE,
                    font_scale=0.52, thickness=1, pad=5):
    """Semi-transparent filled label pill."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), base = cv2.getTextSize(text, font, font_scale, thickness)
    x1, y1 = x, y - th - pad * 2
    x2, y2 = x + tw + pad * 2, y
    overlay = img.copy()
    cv2.rectangle(overlay, (x1, y1), (x2, y2), bg_color, -1)
    cv2.addWeighted(overlay, 0.72, img, 0.28, 0, img)
    cv2.putText(img, text, (x + pad, y - pad),
                font, font_scale, text_color, thickness, cv2.LINE_AA)


def draw_fill_bar(img, x1, x2, y, height, ratio, color, bg=C_GRAY):
    """Horizontal progress bar."""
    cv2.rectangle(img, (x1, y), (x2, y + height), bg, -1)
    fill_w = int((x2 - x1) * min(ratio, 1.0))
    if fill_w > 0:
        cv2.rectangle(img, (x1, y), (x1 + fill_w, y + height), color, -1)


def draw_hud(img, fps, enrolled, active_known, active_unknown):
    """Right-side dark HUD panel."""
    h, w = img.shape[:2]
    x0 = w - HUD_W
    font = cv2.FONT_HERSHEY_SIMPLEX

    overlay = img.copy()
    cv2.rectangle(overlay, (x0, 0), (w, h), C_DARK, -1)
    cv2.addWeighted(overlay, 0.60, img, 0.40, 0, img)

    # Vertical separator line
    cv2.line(img, (x0, 0), (x0, h), C_TEAL, 1)

    def put(text, row, color=C_WHITE, scale=0.42, bold=False):
        cv2.putText(img, text, (x0 + 12, row), font, scale, color,
                    2 if bold else 1, cv2.LINE_AA)

    put("FACE  ID  SYSTEM",  28,  C_TEAL,  0.44, bold=True)
    cv2.line(img, (x0 + 10, 36), (w - 10, 36), C_TEAL, 1)

    put(f"FPS       {fps:5.1f}",   62,  C_WHITE)
    put(f"Enrolled  {enrolled}",    82,  C_WHITE)
    put(f"Known     {active_known}", 102, C_GREEN)
    put(f"Unknown   {active_unknown}", 122, C_RED if active_unknown else C_WHITE)

    cv2.line(img, (x0 + 10, 136), (w - 10, 136), C_GRAY, 1)
    put(datetime.now().strftime("%H:%M:%S"), 158, C_AMBER, scale=0.50, bold=True)
    put(datetime.now().strftime("%Y-%m-%d"), 178, C_GRAY,  scale=0.38)

    cv2.line(img, (x0 + 10, h - 44), (w - 10, h - 44), C_GRAY, 1)
    put("Press Q to quit", h - 28, C_GRAY, scale=0.36)


def draw_scan_line(img, tick, feed_w):
    """Faint animated scan line scrolling down the feed area."""
    h = img.shape[0]
    y = int((tick * 2) % h)
    overlay = img.copy()
    cv2.line(overlay, (0, y), (feed_w, y), (60, 255, 120), 1)
    cv2.addWeighted(overlay, 0.20, img, 0.80, 0, img)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(source, tolerance: float, process_every: int):
    known_names, known_encodings = load_known_faces()
    enrolled_count = len(set(known_names))
    print(f"Loaded {len(known_encodings)} face samples for {enrolled_count} people.")

    cap = cv2.VideoCapture(source if source is not None else 0)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video source: {source or 0}")

    last_alert_time  = 0.0
    last_logged_at   = {}   # name -> float
    frame_count      = 0
    cached_boxes     = []
    cached_names     = []

    # known_sessions[name] = {"first_seen": float, "last_seen": float}
    known_sessions   = {}

    # unknown_flashes = list of {"captured_at": float, "box": (t,r,b,l)}
    unknown_flashes  = []

    fps              = 0.0
    _fps_ts          = time.time()
    _fps_frames      = 0
    tick             = 0

    print("Running — press Q to quit.")

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Frame grab failed, stopping.")
            break

        frame_count += 1
        _fps_frames += 1
        tick += 1
        now = time.time()

        # FPS
        _elapsed = now - _fps_ts
        if _elapsed >= 1.0:
            fps = _fps_frames / _elapsed
            _fps_frames = 0
            _fps_ts = now

        h, w = frame.shape[:2]
        feed_w = w - HUD_W   # usable video area width

        # ── Recognition ───────────────────────────────────────────────────
        if frame_count % process_every == 0:
            small    = cv2.resize(frame, (0, 0), fx=0.25, fy=0.25)
            rgb_sm   = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            boxes_sm = face_recognition.face_locations(rgb_sm, model="hog")
            encs_sm  = face_recognition.face_encodings(rgb_sm, boxes_sm)

            new_names = []
            for enc in encs_sm:
                matches = face_recognition.compare_faces(
                    known_encodings, enc, tolerance=tolerance
                )
                name = "UNKNOWN"
                if True in matches:
                    dists = face_recognition.face_distance(known_encodings, enc)
                    best  = int(dists.argmin())
                    if matches[best]:
                        name = known_names[best]
                new_names.append(name)

                if name == "UNKNOWN":
                    if now - last_alert_time > ALERT_COOLDOWN_SEC:
                        play_alert_async()
                        last_alert_time = now
                        log_event("UNKNOWN")
                else:
                    sess = known_sessions.setdefault(
                        name, {"first_seen": now, "last_seen": now}
                    )
                    sess["last_seen"] = now
                    if now - last_logged_at.get(name, 0) > LOG_COOLDOWN_SEC:
                        log_event(name)
                        last_logged_at[name] = now

            # Scale boxes back to full resolution
            cached_boxes = [
                (t * 4, r * 4, b * 4, l * 4)
                for (t, r, b, l) in boxes_sm
            ]
            cached_names = new_names

            # Register unknown flash entries
            for (t, r, b, l), nm in zip(cached_boxes, new_names):
                if nm == "UNKNOWN":
                    unknown_flashes.append({"captured_at": now, "box": (t, r, b, l)})

        # Expire old unknown flashes
        unknown_flashes = [
            f for f in unknown_flashes
            if now - f["captured_at"] < UNKNOWN_RESET_SEC
        ]

        # Expire known sessions not seen recently
        for nm in list(known_sessions):
            if now - known_sessions[nm]["last_seen"] > SESSION_EXPIRE_SEC:
                del known_sessions[nm]

        # ── Draw ──────────────────────────────────────────────────────────
        draw_scan_line(frame, tick, feed_w)

        # Current detections
        for (top, right, bottom, left), name in zip(cached_boxes, cached_names):
            # Clamp right edge so boxes don't invade the HUD
            right = min(right, feed_w - 2)
            if right <= left:
                continue

            is_known  = (name != "UNKNOWN")
            color     = C_GREEN if is_known else C_RED
            arm       = max(18, (right - left) // 4)

            # Pulse thickness for unknowns
            if is_known:
                thickness = 2
            else:
                thickness = 2 + int(abs(np.sin(tick * 0.18)) * 2)

            draw_corners(frame, left, top, right, bottom, color, thickness, arm)

            # Label position: above box if room, else below
            label_y = top - 8 if top > 30 else bottom + 22
            label_y = max(label_y, 20)

            if is_known:
                sess    = known_sessions.get(name, {})
                dur     = now - sess.get("first_seen", now)
                label   = f"  {name}  [ {fmt_duration(dur)} ]"
                draw_pill_label(frame, label, left, label_y, C_GREEN)

                # Duration fill bar just below the face box
                bar_y = bottom + 6
                draw_fill_bar(frame, left, right, bar_y, 5,
                              dur / 60.0, C_GREEN)
            else:
                draw_pill_label(frame, "  UNKNOWN  ", left, label_y, C_RED)

        # Lingering unknown flash boxes (fading out with "AUTO RESET" text)
        for flash in unknown_flashes:
            age   = now - flash["captured_at"]
            alpha = max(0.0, 1.0 - age / UNKNOWN_RESET_SEC)
            ft, fr, fb, fl = flash["box"]
            fr = min(fr, feed_w - 2)
            if fr <= fl:
                continue

            # Fading red fill
            overlay = frame.copy()
            cv2.rectangle(overlay, (fl, ft), (fr, fb), C_RED, -1)
            cv2.addWeighted(overlay, alpha * 0.22, frame, 1 - alpha * 0.22, 0, frame)

            # Fading brackets
            draw_corners(frame, fl, ft, fr, fb, C_RED, 2)

            # "AUTO RESET" centred text while still strong
            if alpha > 0.45:
                font = cv2.FONT_HERSHEY_SIMPLEX
                msg  = "AUTO  RESET"
                (tw, th), _ = cv2.getTextSize(msg, font, 0.55, 2)
                cx = (fl + fr) // 2 - tw // 2
                cy = (ft + fb) // 2 + th // 2
                cv2.putText(frame, msg, (cx, cy),
                            font, 0.55, C_RED, 2, cv2.LINE_AA)

        # HUD panel
        n_known   = len([n for n in cached_names if n != "UNKNOWN"])
        n_unknown = len([n for n in cached_names if n == "UNKNOWN"])
        draw_hud(frame, fps, enrolled_count, n_known, n_unknown)

        cv2.imshow("Face ID — Employee Recognition", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Real-time employee face recognition — enhanced UI."
    )
    parser.add_argument(
        "--source", default=None,
        help="Video source: webcam index (default 0) or an RTSP/IP camera URL",
    )
    parser.add_argument(
        "--tolerance", type=float, default=0.5,
        help="Lower = stricter matching (default 0.5). Try 0.4–0.6.",
    )
    parser.add_argument(
        "--process-every", type=int, default=2,
        help="Run recognition every N frames for performance (default 2).",
    )
    args = parser.parse_args()
    run(args.source, args.tolerance, args.process_every)
