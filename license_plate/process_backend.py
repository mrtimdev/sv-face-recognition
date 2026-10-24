"""Optional persistent OCR subprocess, isolated from the desktop OpenCV wheel."""
import base64
import json
import logging
import math
from pathlib import Path
import queue
import struct
import subprocess
import threading
from types import SimpleNamespace

import cv2
import numpy as np

MAX_REQUEST_BYTES = 3_000_000
MAX_RESPONSE_BYTES = 262_144
MAX_JPEG_BYTES = 2_000_000
MAX_DETECTIONS = 64


def _read_exact(pipe, length):
    data = bytearray()
    while len(data) < length:
        part = pipe.read(length - len(data))
        if not part:
            raise RuntimeError('Plate OCR process closed its output')
        data.extend(part)
    return bytes(data)


def _read_message(pipe):
    length = struct.unpack('!I', _read_exact(pipe, 4))[0]
    if not 0 < length <= MAX_RESPONSE_BYTES:
        raise RuntimeError('Plate OCR response exceeds protocol limits')
    try:
        value = json.loads(_read_exact(pipe, length))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError('Plate OCR returned invalid JSON') from exc
    if not isinstance(value, dict):
        raise RuntimeError('Plate OCR returned an invalid message')
    return value


class ProcessRecognizer:
    """One request in flight; timeouts kill even a child blocked on pipe I/O."""
    def __init__(self, python, timeout=15, startup_timeout=120, worker_path=None):
        # Preserve venv executable symlinks: resolving them selects the base
        # interpreter and silently loses the isolated environment's packages.
        executable = Path(python).expanduser().absolute()
        if not executable.is_file():
            raise RuntimeError('SV_PLATE_PYTHON must point to the isolated plate environment Python')
        if (not math.isfinite(timeout) or not math.isfinite(startup_timeout)
                or timeout <= 0 or startup_timeout <= 0):
            raise ValueError('Plate process timeouts must be finite and positive')
        self.timeout = timeout
        self._closed = threading.Event()
        self._predict_lock = threading.Lock()
        self._requests = queue.Queue(maxsize=1)
        self._responses = queue.Queue(maxsize=1)
        worker = Path(worker_path) if worker_path else Path(__file__).with_name('process_worker.py')
        try:
            self._process = subprocess.Popen([str(executable), '-I', '-u', str(worker.resolve())],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as exc:
            raise RuntimeError(f'Could not start isolated plate OCR: {exc}') from exc
        self._reader = threading.Thread(target=self._io_loop, name='plate-process-io', daemon=True)
        self._reader.start()
        try:
            ready = self._receive(startup_timeout)
            if ready != {'type':'ready'}:
                raise RuntimeError(ready.get('error', 'Plate OCR startup failed'))
        except BaseException:
            self.close()
            raise

    def _publish(self, response):
        try:
            self._responses.put_nowait(response)
        except queue.Full:
            pass  # Close/timeout already queued the terminal response.

    def _io_loop(self):
        try:
            self._publish(_read_message(self._process.stdout))
            while not self._closed.is_set():
                payload = self._requests.get()
                if payload is None:
                    return
                packet = struct.pack('!I', len(payload)) + payload
                # Raw pipes can return short writes; loop until the whole packet
                # has been delivered. Killing the child unblocks a stalled write.
                view = memoryview(packet)
                while view:
                    written = self._process.stdin.write(view)
                    if not written:
                        raise RuntimeError('Plate OCR input pipe closed')
                    view = view[written:]
                self._publish(_read_message(self._process.stdout))
        except (OSError, RuntimeError, ValueError) as exc:
            self._publish(RuntimeError(f'Isolated plate OCR failed: {exc}'))

    def _receive(self, timeout):
        try:
            response = self._responses.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeError('Isolated plate OCR timed out') from exc
        if isinstance(response, Exception):
            raise response
        if response.get('type') == 'error':
            raise RuntimeError(str(response.get('error', 'Plate OCR failed'))[:1000])
        return response

    def predict(self, frame):
        if not self._predict_lock.acquire(timeout=self.timeout):
            raise RuntimeError('Isolated plate OCR is busy')
        try:
            if self._closed.is_set():
                raise RuntimeError('Isolated plate OCR is closed')
            if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8
                    or frame.ndim != 3 or frame.shape[2] != 3
                    or min(frame.shape[:2]) <= 0 or max(frame.shape[:2]) > 4096):
                raise ValueError('Plate OCR requires an 8-bit color frame no larger than 4096 pixels')
            ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok or len(jpeg) > MAX_JPEG_BYTES:
                raise ValueError('Plate JPEG exceeds 2 MB')
            payload = json.dumps({'type':'predict', 'image':base64.b64encode(jpeg).decode('ascii')},
                                 separators=(',', ':')).encode()
            if len(payload) > MAX_REQUEST_BYTES:
                raise ValueError('Plate request exceeds protocol limits')
            self._requests.put_nowait(payload)
            response = self._receive(self.timeout)
            return self._adapt(response)
        except (RuntimeError, queue.Full):
            self.close()
            raise
        finally:
            self._predict_lock.release()

    @staticmethod
    def _adapt(response):
        rows = response.get('plates')
        if response.get('type') != 'result' or not isinstance(rows, list) or len(rows) > MAX_DETECTIONS:
            raise RuntimeError('Invalid plate OCR response')
        results = []
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError('Invalid plate OCR result')
            text, confidence, box = row.get('text'), row.get('confidence'), row.get('box')
            if (not isinstance(text, str) or len(text) > 128
                    or type(confidence) not in (int, float) or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1 or not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 8192 for v in box)
                    or box[2] <= box[0] or box[3] <= box[1]):
                raise RuntimeError('Invalid plate OCR result fields')
            results.append(SimpleNamespace(ocr=SimpleNamespace(text=text, confidence=confidence),
                detection=SimpleNamespace(bounding_box=SimpleNamespace(**dict(zip(('x1','y1','x2','y2'),box))))))
        return results

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._publish(RuntimeError('Isolated plate OCR is closed'))
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            pass
        process = self._process
        try:
            try:
                running = process.poll() is None
            except OSError:
                running = True
            if running:
                try:
                    process.terminate()
                except OSError:
                    pass  # It may have exited between poll and terminate.
                try:
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass  # Report below, after releasing local resources.
        finally:
            try:
                self._reader.join(timeout=1)
            finally:
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            logging.warning('Could not close isolated plate OCR pipe')
        try:
            running = process.poll() is None
        except OSError:
            running = True
        if running:
            logging.error('Isolated plate OCR process did not exit after terminate/kill; operator recovery is required')
        if self._reader.is_alive():
            self._reader.join(timeout=.2)
            if self._reader.is_alive():
                logging.error('Isolated plate OCR I/O thread did not exit after pipe cleanup')
