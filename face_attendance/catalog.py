"""Versioned SFace enrollment loading and stable employee identifiers."""
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import numpy as np

from .models import Employee
from .template_store import read_templates


def legacy_employee_id(name):
    return "local-" + str(uuid5(NAMESPACE_URL, "face-attendance/" + name))


def load_employee_map(path):
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("employees.json must map enrollment names to employee records")
    result = {}
    for name, record in data.items():
        if not isinstance(record, dict) or not str(record.get("employee_id", "")).strip():
            raise ValueError(f"Missing employee_id for {name!r}")
        result[name] = Employee(str(record["employee_id"]), str(record.get("name", name)))
    return result


class FaceCatalog:
    def __init__(self, encodings_path, employees_path, allow_empty=False):
        self.employee_map = load_employee_map(employees_path)
        path = Path(encodings_path)
        if not path.exists():
            if allow_empty:
                self.employees = ()
                self.encodings = np.zeros((0, 128), dtype=np.float64)
                self.enrolled_count = 0
                return
            raise FileNotFoundError(f"{path} not found. Run enroll_faces.py first.")
        data = read_templates(path)
        employees, encodings = [], []
        for name, samples in data.items():
            employee = self.employee_map.get(name, Employee(legacy_employee_id(name), name))
            self.employee_map[name] = employee
            for sample in samples:
                vector = np.asarray(sample, dtype=np.float64)
                if vector.shape != (128,) or not np.isfinite(vector).all():
                    raise ValueError(f"Invalid face encoding for {name!r}")
                employees.append(employee)
                encodings.append(vector)
        if not encodings:
            if allow_empty:
                self.employees = ()
                self.encodings = np.zeros((0, 128), dtype=np.float64)
                self.enrolled_count = 0
                return
            raise ValueError("No enrolled face samples. Run enroll_faces.py first.")
        self.employees = tuple(employees)
        self.encodings = np.stack(encodings)
        self.enrolled_count = len({e.employee_id for e in employees})

    def duplicate_identities(self):
        """Find exact enrollment duplicates assigned to different employees at startup."""
        seen, conflicts = {}, set()
        for employee, encoding in zip(self.employees, self.encodings):
            key = encoding.tobytes()
            prior = seen.get(key)
            if prior and prior.employee_id != employee.employee_id:
                conflicts.add(tuple(sorted((prior.name, employee.name))))
            else:
                seen[key] = employee
        return sorted(conflicts)
