"""Standalone isolated OCR child. Uses no application modules or Qt imports."""
import base64
import json
import math
import os
import struct
import sys

MAX_REQUEST_BYTES = 3_000_000
MAX_RESPONSE_BYTES = 262_144
MAX_JPEG_BYTES = 2_000_000


def read_exact(pipe, length):
    data = bytearray()
    while len(data) < length:
        chunk = pipe.read(length-len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def send(pipe, payload):
    encoded = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode()
    if not 0 < len(encoded) <= MAX_RESPONSE_BYTES:
        raise ValueError('OCR response exceeds protocol limits')
    packet = memoryview(struct.pack('!I', len(encoded)) + encoded)
    while packet:
        written = pipe.write(packet)
        if not written:
            raise EOFError
        packet = packet[written:]


def check_dimensions(raw):
    if raw[:2] != b'\xff\xd8':
        raise ValueError('Expected a JPEG frame')
    offset, dimensions = 2, None
    while offset < len(raw):
        if raw[offset] != 255:
            break
        while offset < len(raw) and raw[offset] == 255:
            offset += 1
        if offset >= len(raw):
            break
        marker = raw[offset]
        offset += 1
        if marker == 0xDA and dimensions:
            return
        if marker in (0, 0xD8, 0xD9, 0xDA):
            break
        if marker == 1 or 0xD0 <= marker <= 0xD7:
            continue
        if offset+2 > len(raw):
            break
        length = int.from_bytes(raw[offset:offset+2], 'big')
        if length < 2 or offset+length > len(raw):
            break
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if dimensions or length < 8:
                break
            height = int.from_bytes(raw[offset+3:offset+5], 'big')
            width = int.from_bytes(raw[offset+5:offset+7], 'big')
            if not (0 < width <= 4096 and 0 < height <= 4096):
                raise ValueError('Plate image dimensions exceed limits')
            dimensions = (width, height)
        offset += length
    raise ValueError('Invalid JPEG frame')


def main():
    # Keep protocol bytes separate from Python and native-library stdout logs.
    output = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    try:
        import cv2
        import numpy as np
        from fast_alpr import ALPR
        recognizer = ALPR(detector_model='yolo-v9-t-384-license-plate-end2end',
                          ocr_model='cct-xs-v2-global-model', ocr_device='cpu',
                          detector_providers=['CPUExecutionProvider'])
        send(output, {'type':'ready'})
        while True:
            length = struct.unpack('!I', read_exact(sys.stdin.buffer, 4))[0]
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError('Plate request exceeds protocol limits')
            request = json.loads(read_exact(sys.stdin.buffer, length))
            if (not isinstance(request, dict) or request.get('type') != 'predict'
                    or not isinstance(request.get('image'), str)
                    or len(request['image']) > 4*((MAX_JPEG_BYTES+2)//3)):
                raise ValueError('Invalid plate prediction request')
            raw = base64.b64decode(request['image'], validate=True)
            if not 0 < len(raw) <= MAX_JPEG_BYTES:
                raise ValueError('Plate JPEG exceeds limits')
            check_dimensions(raw)
            frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if frame is None or max(frame.shape[:2]) > 4096:
                raise ValueError('Invalid plate image')
            plates = []
            for result in recognizer.predict(frame):
                if result.ocr is None or not result.ocr.text:
                    continue
                values = result.ocr.confidence
                confidence = sum(values)/len(values) if isinstance(values, (list, tuple)) and values else float(values or 0)
                box = result.detection.bounding_box
                text = str(result.ocr.text)
                if len(text) > 128 or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise ValueError('Invalid OCR output')
                plates.append({'text':text, 'confidence':float(confidence),
                               'box':[float(box.x1),float(box.y1),float(box.x2),float(box.y2)]})
                if len(plates) > 64:
                    raise ValueError('Too many plate detections')
            send(output, {'type':'result', 'plates':plates})
    except EOFError:
        pass
    except Exception as exc:
        send(output, {'type':'error', 'error':str(exc)[:1000]})
    finally:
        output.close()


if __name__ == '__main__':
    main()
