"""Persistence and fingerprint helpers."""
import hashlib
import json
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_DIR / ".auto-upload-config.json"
STATE_FILE = PROJECT_DIR / ".auto-upload-state.json"

def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temp.replace(path)

def fingerprint(path: Path) -> str:
    info = path.stat()
    return hashlib.sha256(f"{path}:{info.st_size}:{info.st_mtime_ns}".encode()).hexdigest()
