"""Move enrolled employees between computers in one portable file.

An export is a zip holding ``employees.json`` (format, face-template model and,
per enrollment label, the employee ID, display name, face vectors and photo
names) plus the photos.  Nothing in it is executable: unlike the catalog's
pickle, an import only parses JSON and decodes JPEGs, and the photo names
inside the zip never decide where anything is written.

Import adds employees that aren't enrolled here yet and new samples to ones
that are (same label and ID).  It skips anything that conflicts - above all
an employee ID that already belongs to a different person here, which would
otherwise merge two people's attendance.  Changes go through the same atomic
writers as enrollment; photos written by a failed import are removed again.
"""
import json
import math
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

from .catalog import legacy_employee_id, load_employee_map
from .enrollment import _atomic_write, write_encodings
from .enrollment_photos import photo_directory, photo_path, portrait_name, portrait_path
from .template_store import TEMPLATE_MODEL, read_template_bundle
from .version import __version__

FORMAT = "sv-face-id/employees"
FORMAT_VERSION = 1
MANIFEST = "employees.json"
MAX_FILE_BYTES = 20 * 1024 * 1024       # any one member of the zip
MAX_MEMBERS = 50_000
_PHOTO_MEMBER = re.compile(r"photos/([0-9a-f]{32}\.jpg)")
_PORTRAIT_MEMBER = re.compile(r"portraits/\d{1,6}\.jpg")


class TransferError(Exception):
    """The file can't be exported or imported; the message is shown to the user."""


@dataclass
class ImportReport:
    added: list = field(default_factory=list)      # display names of new employees
    updated: list = field(default_factory=list)    # existing employees that got new samples
    samples: int = 0
    skipped: list = field(default_factory=list)    # "Name: reason"

    @property
    def changed(self):
        return bool(self.added or self.updated)

    def summary(self):
        parts = []
        if self.added:
            parts.append(f"Added {_count(len(self.added), 'employee')}")
        if self.updated:
            parts.append(f"added samples to {_count(len(self.updated), 'employee')}")
        text = (", ".join(parts) + f" ({_count(self.samples, 'face sample')}).") if parts else \
            "Nothing new to import: these employees are already here."
        if self.skipped:
            text += " Skipped " + "; ".join(self.skipped) + "."
        return text[0].upper() + text[1:]


def _count(number, noun):
    return f"{number} {noun}{'' if number == 1 else 's'}"


# --- export ------------------------------------------------------------------

def export_employees(destination, encodings_path, employees_path):
    """Write every enrolled employee to *destination* (a .zip); returns (employees, samples)."""
    samples, refs = read_template_bundle(encodings_path)
    if not samples:
        raise TransferError("There are no enrolled employees to export.")
    records = load_employee_map(employees_path)
    labels = list(samples)
    entries, files, total = [], {}, 0
    for index, label in enumerate(labels, start=1):
        item = {"label": label, "samples": []}
        if label in records:
            item["employee_id"] = records[label].employee_id
            item["name"] = records[label].name
        for vector, reference in zip(samples[label], refs.get(label, [])):
            source = photo_path(encodings_path, reference)
            member = f"photos/{reference}" if source is not None and source.is_file() else None
            if member:
                files[member] = source
            values = np.asarray(vector, dtype=np.float64).reshape(-1)
            item["samples"].append({"vector": [float(value) for value in values], "photo": member})
            total += 1
        portrait = portrait_path(encodings_path, label, labels)
        if portrait is not None:
            item["portrait"] = f"portraits/{index}.jpg"
            files[item["portrait"]] = portrait
        entries.append(item)
    manifest = {"format": FORMAT, "version": FORMAT_VERSION, "model": TEMPLATE_MODEL,
                "app_version": __version__,
                "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "employees": entries}
    destination = Path(destination)
    partial = destination.with_name(destination.name + ".part")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
            for member, source in files.items():
                archive.write(source, member)
        os.replace(partial, destination)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise TransferError(f"Couldn't write {destination.name}: {exc.strerror or exc}") from exc
    return len(entries), total


# --- import ------------------------------------------------------------------

def import_employees(source, encodings_path, employees_path):
    """Add the employees in *source* to this computer's catalog; returns an ``ImportReport``."""
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise TransferError("This isn't an SV Face ID employee file.") from exc
    with archive:
        if len(archive.infolist()) > MAX_MEMBERS:
            raise TransferError("This file has too many entries to be an employee export.")
        manifest = _read_manifest(archive)
        return _merge(archive, manifest["employees"], Path(encodings_path), Path(employees_path))


def _read_member(archive, name):
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_FILE_BYTES:
        raise TransferError(f"{name} in this file is too large.")
    with archive.open(info) as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise TransferError(f"{name} in this file is too large.")
    return data


def _read_manifest(archive):
    data = _read_member(archive, MANIFEST)
    try:
        manifest = json.loads(data.decode("utf-8")) if data is not None else None
    except (UnicodeDecodeError, ValueError):
        manifest = None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise TransferError("This isn't an SV Face ID employee file.")
    if manifest.get("version") != FORMAT_VERSION:
        raise TransferError("This employee file was made by a newer version of SV Face ID. "
                            "Update the app, then import it again.")
    if manifest.get("model") != TEMPLATE_MODEL:
        raise TransferError("This file's face data comes from a different recognition model, "
                            "so it can't be used here. Re-enroll these employees instead.")
    if not isinstance(manifest.get("employees"), list):
        raise TransferError("This employee file is damaged.")
    return manifest


def _vector(value):
    if not isinstance(value, list) or len(value) != 128:
        return None
    try:
        vector = np.asarray([float(number) for number in value], dtype=np.float64)
    except (TypeError, ValueError):
        return None
    return vector if all(math.isfinite(number) for number in vector) else None


def _image(data):
    if not data:
        return False
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR) is not None


def _write_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, lambda handle: handle.write(data))


def _merge(archive, items, encodings_path, employees_path):
    samples, refs = read_template_bundle(encodings_path)
    records = load_employee_map(employees_path)
    owners = {}             # employee ID -> display name already enrolled here
    for label in set(samples) | set(records):
        record = records.get(label)
        employee_id = record.employee_id if record else legacy_employee_id(label)
        owners.setdefault(employee_id, record.name if record else label)
    labels_here = list(samples)
    report, written, new_records = ImportReport(), [], {}
    photo_dir = photo_directory(encodings_path)
    try:
        for item in items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            if not label:
                report.skipped.append("an entry without a name")
                continue
            given_id = str(item.get("employee_id") or "").strip()
            name = str(item.get("name") or label).strip() or label
            employee_id = given_id or legacy_employee_id(label)
            here = records.get(label)
            id_here = here.employee_id if here else (legacy_employee_id(label) if label in samples else None)
            if id_here is not None and id_here != employee_id:
                report.skipped.append(f"{name}: already enrolled here with ID {id_here}")
                continue
            owner = owners.get(employee_id)
            if id_here is None and owner is not None and owner.casefold() != name.casefold():
                report.skipped.append(f"{name}: ID {employee_id} already belongs to {owner}")
                continue
            known = {np.asarray(vector, dtype=np.float64).reshape(-1).tobytes()
                     for vector in samples.get(label, [])}
            fresh = []
            for entry in item.get("samples") or []:
                vector = _vector(entry.get("vector")) if isinstance(entry, dict) else None
                if vector is None or vector.tobytes() in known:
                    continue
                known.add(vector.tobytes())
                fresh.append((vector, entry.get("photo")))
            if not fresh:
                if id_here is None:
                    report.skipped.append(f"{name}: no usable face samples")
                continue
            for vector, member in fresh:
                reference = None
                match = _PHOTO_MEMBER.fullmatch(member) if isinstance(member, str) else None
                data = _read_member(archive, member) if match else None
                if _image(data):
                    reference = match.group(1)
                    target = photo_dir / reference
                    if target.exists() and target.read_bytes() != data:
                        reference = uuid4().hex + ".jpg"
                        target = photo_dir / reference
                    if not target.exists():
                        _write_file(target, data)
                        written.append(target)
                samples.setdefault(label, []).append(vector)
                refs.setdefault(label, []).append(reference)
            portrait = item.get("portrait")
            if isinstance(portrait, str) and _PORTRAIT_MEMBER.fullmatch(portrait) \
                    and portrait_path(encodings_path, label, labels_here + [label]) is None:
                data = _read_member(archive, portrait)
                if _image(data):
                    target = encodings_path.parent / "enrollment_photos" / portrait_name(label)
                    _write_file(target, data)
                    written.append(target)
            if given_id and here is None:
                new_records[label] = {"employee_id": given_id, "name": name}
            owners.setdefault(employee_id, name)
            (report.updated if id_here is not None else report.added).append(name)
            report.samples += len(fresh)
        if not report.changed:
            return report
        previous = employees_path.read_bytes() if new_records and employees_path.exists() else None
        if new_records:
            mapping = {key: {"employee_id": value.employee_id, "name": value.name}
                       for key, value in records.items()}
            mapping.update(new_records)
            _atomic_write(employees_path, lambda handle: json.dump(mapping, handle, ensure_ascii=False,
                                                                     indent=2), binary=False)
        try:
            write_encodings(encodings_path, samples, photos=refs)
        except Exception:
            if new_records:          # put the mapping back the way it was
                if previous is None:
                    employees_path.unlink(missing_ok=True)
                else:
                    _atomic_write(employees_path, lambda handle: handle.write(previous))
            raise
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return report
