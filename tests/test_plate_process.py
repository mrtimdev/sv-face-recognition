"""Bounded persistent OCR IPC without downloading or executing OCR models."""
import io
import json
from pathlib import Path
import struct
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from license_plate.core import create_recognizer, read_plates
from license_plate.process_backend import ProcessRecognizer, MAX_RESPONSE_BYTES, _read_message

FAKE_CHILD = '''
import json, struct, sys, time
mode = MODE
out = sys.stdout.buffer

def send(value):
    data = json.dumps(value).encode()
    out.write(struct.pack('!I', len(data)) + data)
    out.flush()

def read_exact(n):
    data = bytearray()
    while len(data) < n:
        value = sys.stdin.buffer.read(n-len(data))
        if not value: raise EOFError
        data.extend(value)
    return bytes(data)

if mode == 'startup_hang': time.sleep(30)
if mode == 'startup_error':
    send({'type':'error','error':'model unavailable'})
    sys.exit()
if mode == 'oversized':
    out.write(struct.pack('!I', 99999999)); out.flush()
    time.sleep(30)
if mode == 'malformed':
    out.write(struct.pack('!I', 1) + b'{'); out.flush()
    time.sleep(30)
send({'type':'ready'})
if mode == 'blocked_writer': time.sleep(30)
try:
    while True:
        size = struct.unpack('!I', read_exact(4))[0]
        request = json.loads(read_exact(size))
        assert request['type'] == 'predict'
        assert len(request['image']) < 3000000
        if mode == 'predict_hang': time.sleep(30)
        if mode == 'bad_result':
            send({'type':'result','plates':[{'text':'X','confidence':float('nan'),'box':[1,2,3,4]}]})
        elif mode == 'request_error':
            send({'type':'error','error':'prediction failed'})
        else:
            send({'type':'result','plates':[{'text':'AB123','confidence':.95,'box':[1,2,30,20]}]})
except EOFError:
    pass
'''


def spawn(tmp_path, mode='normal', **kwargs):
    worker = tmp_path / (mode + '.py')
    worker.write_text(FAKE_CHILD.replace('mode = MODE', 'mode = ' + repr(mode)), encoding='utf-8')
    return ProcessRecognizer(sys.executable, worker_path=worker, **kwargs)


def test_predict_adapter_persistent_process_and_clean_close(tmp_path):
    recognizer = spawn(tmp_path)
    frame = np.zeros((100, 120, 3), np.uint8)
    try:
        process = recognizer._process
        for _ in range(2):
            plates = read_plates(recognizer, frame, .9)
            assert plates[0].text == 'AB123'
            assert plates[0].box == (1, 2, 30, 20)
            assert recognizer._process is process
    finally:
        recognizer.close()
        recognizer.close()
    assert recognizer._process.poll() is not None
    assert not recognizer._reader.is_alive()
    with pytest.raises(RuntimeError, match='closed'):
        recognizer.predict(frame)


@pytest.mark.parametrize('mode', ['startup_hang', 'startup_error', 'oversized', 'malformed'])
def test_startup_failures_terminate_process(tmp_path, mode):
    import subprocess
    processes = []
    original = subprocess.Popen
    def start(*args, **kwargs):
        result = original(*args, **kwargs)
        processes.append(result)
        return result
    with patch('license_plate.process_backend.subprocess.Popen', side_effect=start):
        with pytest.raises(RuntimeError):
            spawn(tmp_path, mode, startup_timeout=.2)
    assert processes[0].poll() is not None


@pytest.mark.parametrize('mode', ['predict_hang', 'blocked_writer'])
def test_prediction_deadline_kills_hung_or_nonreading_child(tmp_path, mode):
    recognizer = spawn(tmp_path, mode, timeout=.2)
    frame = np.random.default_rng(1).integers(0, 255, (480, 640, 3), dtype=np.uint8)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='timed out'):
        recognizer.predict(frame)
    assert time.monotonic()-started < 3
    assert recognizer._process.poll() is not None
    assert not recognizer._reader.is_alive()


@pytest.mark.parametrize('mode', ['bad_result', 'request_error'])
def test_invalid_result_or_child_error_closes_process(tmp_path, mode):
    recognizer = spawn(tmp_path, mode)
    with pytest.raises(RuntimeError):
        recognizer.predict(np.zeros((100, 120, 3), np.uint8))
    assert recognizer._process.poll() is not None
    assert not recognizer._reader.is_alive()


def test_response_length_is_checked_before_reading_payload():
    with pytest.raises(RuntimeError, match='limits'):
        _read_message(io.BytesIO(struct.pack('!I', MAX_RESPONSE_BYTES+1)))


def test_invalid_frames_are_rejected_without_poisoning_child(tmp_path):
    recognizer = spawn(tmp_path)
    try:
        for frame in [np.zeros((2, 2), np.uint8), np.zeros((2, 2, 3), np.float32),
                      np.zeros((1, 4097, 3), np.uint8)]:
            with pytest.raises(ValueError):
                recognizer.predict(frame)
        assert recognizer.predict(np.zeros((100, 120, 3), np.uint8))
    finally:
        recognizer.close()


def test_environment_selects_isolated_backend_without_inline_import(monkeypatch):
    monkeypatch.setenv('SV_PLATE_PYTHON', '/absolute/plate-venv/bin/python')
    value = object()
    with patch('license_plate.process_backend.ProcessRecognizer', return_value=value) as factory:
        assert create_recognizer() is value
        factory.assert_called_once_with('/absolute/plate-venv/bin/python')


def test_worker_script_header_validation_has_no_application_imports():
    import runpy
    source = Path(__file__).resolve().parents[1] / 'license_plate' / 'process_worker.py'
    worker = runpy.run_path(str(source), run_name='worker_test')
    with pytest.raises(ValueError):
        worker['check_dimensions'](b'not-a-jpeg')


def test_selected_venv_python_symlink_is_not_resolved_away(tmp_path):
    executable = tmp_path / 'venv-python'
    executable.symlink_to(sys.executable)
    worker = tmp_path / 'child.py'
    worker.write_text(FAKE_CHILD.replace('mode = MODE', "mode = 'normal'"))
    import subprocess
    original = subprocess.Popen
    with patch('license_plate.process_backend.subprocess.Popen', wraps=original) as launch:
        recognizer = ProcessRecognizer(str(executable), worker_path=worker)
        try:
            assert launch.call_args.args[0][0] == str(executable)
        finally:
            recognizer.close()


def test_plate_worker_finally_closes_recognizer_on_prediction_error():
    from face_attendance.config import Config
    from license_plate.screen import PlateWorker
    closed = []
    class Recognizer:
        def close(self):
            closed.append(True)
    packet = SimpleNamespace(sequence=1, generation=1, captured_at=time.monotonic(),
                             frame=np.zeros((20, 40, 3), np.uint8))
    worker = PlateWorker(Config(), '.', .8, 1, 30, 0,
                         shared=lambda: packet, recognizer_factory=Recognizer)
    with patch('license_plate.screen.read_plates', side_effect=RuntimeError('prediction failed')):
        worker.start()
        assert worker.wait(3000)
    assert closed == [True]


def test_oversized_encoded_input_is_rejected_before_ipc(tmp_path):
    recognizer = spawn(tmp_path)
    try:
        with patch('license_plate.process_backend.cv2.imencode', return_value=(True, np.zeros(2_000_001, np.uint8))):
            with pytest.raises(ValueError, match='2 MB'):
                recognizer.predict(np.zeros((20, 40, 3), np.uint8))
        assert recognizer.predict(np.zeros((100, 120, 3), np.uint8))
    finally:
        recognizer.close()


def mock_recognizer(process):
    import queue
    import threading
    from unittest.mock import MagicMock
    recognizer = ProcessRecognizer.__new__(ProcessRecognizer)
    recognizer._closed = threading.Event()
    recognizer._responses = queue.Queue(maxsize=1)
    recognizer._requests = queue.Queue(maxsize=1)
    recognizer._process = process
    recognizer._reader = MagicMock()
    recognizer._reader.is_alive.return_value = False
    return recognizer


def test_close_termination_race_always_releases_reader_and_pipes():
    from unittest.mock import MagicMock
    process = MagicMock()
    process.poll.side_effect = [None, 0]
    process.terminate.side_effect = ProcessLookupError('already exited')
    recognizer = mock_recognizer(process)
    recognizer.close()
    process.wait.assert_called_once_with(timeout=1)
    recognizer._reader.join.assert_called_once_with(timeout=1)
    process.stdin.close.assert_called_once()
    process.stdout.close.assert_called_once()


def test_close_timeout_and_kill_failure_report_without_skipping_cleanup(caplog):
    import subprocess
    from unittest.mock import MagicMock
    process = MagicMock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired('fake', 1)
    process.kill.side_effect = OSError('kill unavailable')
    recognizer = mock_recognizer(process)
    recognizer.close()
    assert process.wait.call_count == 2
    process.kill.assert_called_once()
    recognizer._reader.join.assert_called_once_with(timeout=1)
    process.stdin.close.assert_called_once()
    process.stdout.close.assert_called_once()
    assert 'did not exit after terminate/kill' in caplog.text


def test_cleanup_failure_does_not_mask_prediction_timeout(tmp_path, caplog):
    recognizer = spawn(tmp_path, 'predict_hang', timeout=.1)
    real_process = recognizer._process
    from unittest.mock import MagicMock
    process = MagicMock()
    process.poll.return_value = None
    process.terminate.side_effect = OSError('cannot terminate')
    process.wait.side_effect = OSError('cannot reap')
    process.kill.side_effect = OSError('cannot kill')
    # Preserve actual I/O so cleanup still closes the live reader's pipe.
    process.stdin, process.stdout = real_process.stdin, real_process.stdout
    recognizer._process = process
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            recognizer.predict(np.zeros((100, 120, 3), np.uint8))
        assert real_process.stdin.closed and real_process.stdout.closed
        assert 'did not exit after terminate/kill' in caplog.text
    finally:
        real_process.kill()
        real_process.wait(timeout=2)
        recognizer._reader.join(timeout=2)


def test_one_pipe_close_failure_does_not_skip_other_pipe(caplog):
    from unittest.mock import MagicMock
    process = MagicMock()
    process.poll.return_value = 0
    process.stdin.close.side_effect = OSError('pipe close failed')
    recognizer = mock_recognizer(process)
    recognizer.close()
    process.stdout.close.assert_called_once()
    assert 'Could not close isolated plate OCR pipe' in caplog.text
