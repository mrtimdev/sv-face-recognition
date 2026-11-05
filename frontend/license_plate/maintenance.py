"""Local fleet registry. Matching deliberately avoids fuzzy OCR substitutions."""
import json
import re
from datetime import date, timedelta
from pathlib import Path


def plate_key(text):
    return re.sub(r"[\W_]", "", text.upper())


def maintenance_status(record, today=None):
    today = today or date.today()
    due = date.fromisoformat(record["due_date"])
    if due < today:
        return "OVERDUE"
    if due <= today + timedelta(days=7):
        return "DUE SOON"
    return "CLEAR"


def demo_fleet(today=None):
    today = today or date.today()
    return [dict(plate=plate, truck=truck, task=task,
                 due_date=(today + timedelta(days=days)).isoformat())
            for plate, truck, task, days in (
                ("3A-1234", "Demo Truck 01", "Oil and filter change", -3),
                ("3B-5678", "Demo Truck 02", "Brake inspection", 3),
                ("3C-9012", "Demo Truck 03", "Scheduled service", 30))]


def save_fleet(path, records):
    keys = set()
    for record in records:
        key = plate_key(record["plate"])
        if not key or not record["truck"].strip() or not record["task"].strip():
            raise ValueError("Each row needs a plate, truck and service task.")
        date.fromisoformat(record["due_date"])
        if key in keys:
            raise ValueError("Each license plate must be unique.")
        keys.add(key)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_fleet(path):
    path = Path(path)
    records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(records, list):
        raise ValueError("Maintenance list must contain rows.")
    for record in records:
        for key in ("plate", "truck", "task", "due_date"):
            if not isinstance(record[key], str):
                raise ValueError("Maintenance fields must contain text.")
        maintenance_status(record)
    return records
