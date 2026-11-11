"""Optional aggregate heartbeat for unattended deployments; no employee data."""
import json
import os
from pathlib import Path
import tempfile
import time


def write_health(path, stats, frame_age, workers_alive):
    path = Path(path)
    payload = {key: stats.get(key) for key in
               ('status', 'paused', 'storage_ready', 'queue', 'queue_max')}
    payload.update(updated_at=time.time(), frame_age=frame_age,
                   workers_alive=workers_alive, recognition_ok=not bool(stats.get('recognition_error')))
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, allow_nan=False)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def healthy(payload, now=None):
    try:
        return (0 <= (time.time() if now is None else now) - payload['updated_at'] <= 20
                and payload['status'] == 'CONNECTED' and not payload['paused']
                and payload['storage_ready'] and payload['workers_alive'] and payload['recognition_ok']
                and 0 <= payload['frame_age'] <= 3
                and payload['queue'] < payload['queue_max'])
    except (KeyError, TypeError, ValueError):
        return False


def main():
    try:
        payload = json.loads(Path(os.environ['SV_FACE_HEALTH_FILE']).read_text())
        return 0 if healthy(payload) else 1
    except (OSError, ValueError, KeyError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
