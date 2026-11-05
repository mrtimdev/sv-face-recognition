"""Versioned SFace enrollment loading and stable employee identifiers."""
import json
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import numpy as np

from .models import Employee
from .angle_templates import vector_of
from .template_store import read_templates


def legacy_employee_id(name):
    return "local-" + str(uuid5(NAMESPACE_URL, "face-attendance/" + name))


def employee_record(employee, **changes):
    """Serialize one employee for employees.json, keeping optional name parts."""
    record = {"employee_id": employee.employee_id, "name": employee.name, "status": employee.status,
              "first_name": employee.first_name, "last_name": employee.last_name, "gender": employee.gender}
    record.update(changes)
    return {key: value for key, value in record.items()
            if value or key in ("employee_id", "name", "status")}


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
        status = record.get("status", "active")
        if status not in ("active", "inactive"):
            raise ValueError(f"Invalid employment status for {name!r}")
        result[name] = Employee(str(record["employee_id"]), str(record.get("name", name)), status == "active",
                                gender=str(record.get("gender", "")).strip().lower(),
                                first_name=str(record.get("first_name", "")).strip(),
                                last_name=str(record.get("last_name", "")).strip())
    # One employment status per permanent ID, including legacy aliases.
    inactive = {e.employee_id for e in result.values() if not e.active}
    result = {label: replace(e, active=e.employee_id not in inactive) for label, e in result.items()}
    return result


class FaceCatalog:
    def __init__(self, encodings_path, employees_path, allow_empty=False, resolve=None, angle_store=None):
        """*resolve* maps each ``Employee`` to its current details (see hrm_directory.resolver).

        With *angle_store* (see angle_templates) faces are matched only against the HRM angles,
        and the local samples in *encodings_path* aren't used at all.
        """
        resolve = resolve or (lambda employee: employee)
        self.employee_map = {label: resolve(employee) for label, employee in load_employee_map(employees_path).items()}
        if angle_store is not None:
            self._load_angles(angle_store, resolve)
            return
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
        inactive_ids = {e.employee_id for e in self.employee_map.values() if not e.active}
        for name, samples in data.items():
            employee_id = legacy_employee_id(name)
            employee = self.employee_map.get(name) or resolve(
                Employee(employee_id, name, employee_id not in inactive_ids))
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

    def _load_angles(self, store, resolve):
        known = {employee.employee_id.strip().casefold(): employee for employee in self.employee_map.values()}
        employees, encodings = [], []
        for code, angles in store.items():
            vectors = [vector for vector in map(vector_of, angles.values()) if vector is not None]
            if not vectors:
                continue
            employee = known.get(str(code).strip().casefold())
            if employee is None:
                employee = resolve(Employee(str(code), str(code)))
                self.employee_map[str(code)] = employee
            employees.extend([employee] * len(vectors))
            encodings.extend(vectors)
        self.employees = tuple(employees)
        self.encodings = np.stack(encodings) if encodings else np.zeros((0, 128), dtype=np.float64)
        self.enrolled_count = len({employee.employee_id for employee in employees})

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
