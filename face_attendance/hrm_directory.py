"""The HRM employee directory, merged with local face enrollments.

HRM (svhrm) is the source of truth for who works here; this app only knows who
has enrolled a face. ``sync`` downloads every HRM employee once into a small
JSON cache next to employees.json, so the Employees screen opens instantly and
still works when HRM is offline. ``merge`` joins that cache with the local
enrollment rows by employee ID; ``Filters`` and ``paginate`` drive the table.

No Qt here; the dashboard runs ``sync`` on a worker thread.
"""
import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import angle_templates, hrm_sync
from .ai_providers import AI_ANGLES_DIR, ANGLES
from .catalog import FaceCatalog

CACHE_NAME = "hrm_employees.json"
PHOTO_DIR = "hrm_photos"
CACHE_VERSION = 2
OLD_CACHE_VERSION = 1   # Listed every employment status and no employee type.
PAGE_SIZES = (25, 50, 100, 200)

ANY = ""
# Enrollment filter values. NOT_IN_HRM picks local enrollments HRM doesn't list as active.
READY, NEEDS, DUPLICATE, LOCAL_ACTIVE, LOCAL_INACTIVE = "ready", "needs", "duplicate", "active", "inactive"
NOT_IN_HRM = "NOT_IN_HRM"


def cache_path(settings):
    return Path(settings.employees_path).with_name(CACHE_NAME)


def photo_dir(settings):
    return Path(settings.employees_path).with_name(PHOTO_DIR)


def _text(value):
    return " ".join(str(value or "").split())


def display_name(row):
    """The name HRM shows: first, middle and last name, else the Latin or nick name."""
    parts = [_text(row.get(key)) for key in ("firstName", "middleName", "lastName")]
    name = " ".join(part for part in parts if part)
    return name or _text(row.get("latinName")) or _text(row.get("nickName")) or _text(row.get("employeeCode"))


def normalize(row):
    """One HRM API row in the app's own field names."""
    code = _text(row.get("employeeCode"))
    status = _text(row.get("status")).upper() or "ACTIVE"
    if row.get("terminated") and status == "ACTIVE":
        status = "TERMINATED"
    return {"hrm_id": row.get("id"),
            "employee_id": code,
            "employee_name": display_name(row),
            "first_name": _text(row.get("firstName")),
            "last_name": _text(row.get("lastName")),
            "latin_name": _text(row.get("latinName")),
            "nick_name": _text(row.get("nickName")),
            "department": _text(row.get("departmentName")),
            "department_code": _text(row.get("departmentCode")),
            "position": _text(row.get("positionName")),
            "location": _text(row.get("locationName")),
            "cost_center": _text(row.get("costCenterName")),
            "hrm_status": status,
            "employee_type": _text(row.get("employeeType")).upper(),
            "has_photo": bool(row.get("hasPhoto"))}


def _atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class Directory:
    employees: tuple = ()
    fetched_at: float = 0.0
    source: str = ""
    outdated: bool = False      # From an older cache: replace it at the next chance.

    @property
    def synced(self):
        return self.fetched_at > 0

    def age_text(self, now=None):
        if not self.synced:
            return "never"
        seconds = max(0, (now or time.time()) - self.fetched_at)
        if seconds < 90:
            return "just now"
        if seconds < 5400:
            return f"{round(seconds / 60)} min ago"
        if seconds < 129600:
            return f"{round(seconds / 3600)} h ago"
        return time.strftime("%d %b %Y", time.localtime(self.fetched_at))


def load(path):
    """The cached directory, or an empty one when there is no usable cache."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Directory()
    if not isinstance(data, dict) or data.get("version") not in (CACHE_VERSION, OLD_CACHE_VERSION):
        return Directory()
    rows = tuple(row for row in data.get("employees") or () if isinstance(row, dict) and row.get("employee_id"))
    outdated = data["version"] != CACHE_VERSION
    if outdated:
        # Still usable, so nobody who has left can check in while HRM is out of reach.
        rows = tuple(row for row in rows if row.get("hrm_status") == "ACTIVE")
    return Directory(rows, float(data.get("fetched_at") or 0), str(data.get("source") or ""), outdated)


def sync(config, path, opener=None, progress=None, now=None):
    """Download the active HRM employees and replace the cache. Raises ``hrm_sync.HRMError``."""
    problem = hrm_sync.configuration_problem(config)
    if problem:
        raise hrm_sync.HRMError(problem.replace("send angles to HRM", "load employees from HRM"))
    rows = [normalize(row) for row in hrm_sync.fetch_all_employees(config, opener=opener, progress=progress)]
    # A row can say ACTIVE and terminated at once; normalize calls that TERMINATED.
    rows = [row for row in rows if row["employee_id"] and row["hrm_status"] == "ACTIVE"]
    directory = Directory(tuple(rows), now or time.time(), config.base_url.strip().rstrip("/"))
    _atomic_json(path, {"version": CACHE_VERSION, "fetched_at": directory.fetched_at,
                        "source": directory.source, "employees": list(directory.employees)})
    return directory


def _key(employee_id):
    return str(employee_id or "").strip().casefold()


def resolver(directory):
    """For the Live Monitor: a function giving an enrolled ``Employee`` their HRM details.

    The name and status come from HRM. Someone HRM doesn't list, or doesn't list as
    ACTIVE, can't record attendance, and marking a person inactive here still holds
    whatever HRM says. Returns None with no usable directory, so a computer that has
    never reached HRM keeps its local list rather than blocking everyone.
    """
    if not directory.synced or not directory.employees:
        return None
    hrm = {}
    for row in directory.employees:
        hrm.setdefault(_key(row["employee_id"]), row)

    def resolve(employee):
        info = hrm.get(_key(employee.employee_id))
        if info is None:
            return replace(employee, active=False)
        return replace(employee, name=info.get("employee_name") or employee.name,
                       first_name=info.get("first_name") or employee.first_name,
                       last_name=info.get("last_name") or employee.last_name,
                       active=employee.active and info.get("hrm_status") == "ACTIVE")
    return resolve


def live_catalog(config, allow_empty=False):
    """The face catalog attendance must use, for both the dashboard and the terminal.

    Connected to HRM it matches only the enrolled angles and applies HRM's names and statuses.
    A computer that has never reached HRM has no angles to use, so it keeps its local samples.
    """
    resolve = resolver(load(cache_path(config)))
    angle_store = None if resolve is None else angle_templates.load(angle_templates.store_path(config))
    return FaceCatalog(config.encodings_path, config.employees_path, allow_empty=allow_empty,
                       resolve=resolve, angle_store=angle_store)


def fingerprint(directory):
    """Changes only when something the Live Monitor uses changes, not on every sync."""
    rows = sorted((row.get("employee_id"), row.get("employee_name"), row.get("first_name"), row.get("last_name"),
                   row.get("hrm_status")) for row in directory.employees)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def face_count(row):
    """Enrolled angles when matching on angles, else local samples."""
    return row.get("angles", row.get("samples", 0)) or 0


def merge(enrollment_rows, directory, angle_counts=None):
    """Every HRM employee plus every local enrollment, joined by employee ID.

    Enrollment rows keep all their keys and gain the HRM fields; an HRM employee
    with no enrollment becomes a row with ``enrolled`` False and 0 samples.
    Local enrollments HRM doesn't list keep ``in_hrm`` False, and ``hrm_blocked`` is
    True when ``resolver`` stops them recording attendance for that reason.
    With *angle_counts* ({casefolded code: usable angles}) every row also gains ``angles``.
    """
    usable = directory.synced and bool(directory.employees)
    hrm = {}
    for row in directory.employees:
        hrm.setdefault(_key(row["employee_id"]), row)
    merged, enrolled_ids = [], set()
    for row in enrollment_rows:
        info = hrm.get(_key(row.get("employee_id")))
        enrolled_ids.add(_key(row.get("employee_id")))
        combined = dict(row)
        combined.update(enrolled=True, in_hrm=info is not None, hrm_blocked=usable and info is None)
        if angle_counts is not None:
            combined["angles"] = angle_counts.get(_key(row.get("employee_id")), 0)
        for field in ("department", "position", "location", "cost_center", "hrm_status", "employee_type",
                      "has_photo", "hrm_id"):
            combined[field] = info.get(field, "") if info else ""
        if info:
            # HRM owns the name, so this table agrees with what the Live Monitor shows.
            for field in ("employee_name", "first_name", "last_name"):
                combined[field] = info.get(field) or combined.get(field, "")
        merged.append(combined)
    for key, info in hrm.items():
        if key in enrolled_ids:
            continue
        row = dict(info)
        row.update(name="", samples=0, enrolled=False, in_hrm=True, mapped=False,
                   active=info.get("hrm_status") == "ACTIVE", status=info.get("hrm_status", "").lower())
        if angle_counts is not None:
            row["angles"] = angle_counts.get(key, 0)
        merged.append(row)
    merged.sort(key=lambda r: (str(r.get("employee_name", "")).casefold(), str(r.get("employee_id", "")).casefold(),
                               str(r.get("name", "")).casefold()))
    return merged


def options(rows, field):
    """Sorted distinct non-empty values of *field*, for a filter box."""
    return sorted({str(row.get(field) or "") for row in rows} - {""}, key=str.casefold)


SEARCH_FIELDS = ("employee_name", "first_name", "last_name", "employee_id", "name", "latin_name", "nick_name",
                 "department", "position", "location")


@dataclass(frozen=True)
class Filters:
    search: str = ""
    department: str = ANY
    position: str = ANY
    location: str = ANY
    employee_type: str = ANY
    enrollment: str = ANY

    def matches(self, row, duplicate_labels=()):
        text = self.search.casefold().strip()
        if text and not any(text in str(row.get(key) or "").casefold() for key in SEARCH_FIELDS):
            return False
        for field in ("department", "position", "location", "employee_type"):
            wanted = getattr(self, field)
            if wanted and str(row.get(field) or "") != wanted:
                return False
        enrolled = face_count(row) > 0
        active = row.get("active", True) and not row.get("hrm_blocked")
        return {ANY: True,
                READY: enrolled and active,
                NEEDS: not enrolled,
                DUPLICATE: bool(row.get("name")) and row.get("name") in duplicate_labels,
                LOCAL_ACTIVE: active,
                LOCAL_INACTIVE: not active,
                NOT_IN_HRM: not row.get("in_hrm")}.get(self.enrollment, True)

    def apply(self, rows, duplicate_labels=()):
        return [row for row in rows if self.matches(row, duplicate_labels)]

    @property
    def active(self):
        return any((self.search.strip(), self.department, self.position, self.location,
                    self.employee_type, self.enrollment))


def paginate(rows, page, size):
    """(rows on that page, the page actually shown, page count). Pages count from 0."""
    size = max(1, int(size))
    pages = max(1, math.ceil(len(rows) / size))
    page = min(max(0, int(page)), pages - 1)
    return rows[page * size:(page + 1) * size], page, pages


def saved_angles_dir(settings, employee_id):
    """Where this employee's angles from HRM are kept, apart from generation runs."""
    digest = hashlib.sha1(str(employee_id or "").strip().encode("utf-8")).hexdigest()[:20]
    return Path(settings.encodings_path).parent / AI_ANGLES_DIR / "hrm" / digest


def _write_private(path, data):
    handle, temp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def fetch_saved_angles(config, settings, employee_id, opener=None):
    """Bring this employee's stored angles down from HRM; an unchanged image isn't downloaded again.

    Returns ([{"key", "path", "version"}, ...], [problem, ...]). One bad image doesn't stop the
    others. Raises ``hrm_sync.HRMError`` when HRM can't list them, ``OSError`` when they can't be saved.
    """
    stored = hrm_sync.list_angles(config, employee_id, opener=opener).get("angles") or []
    known = {angle.key for angle in ANGLES}
    folder = saved_angles_dir(settings, employee_id)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    items, problems, present = [], [], set()
    for row in stored:
        key = row.get("angle") if isinstance(row, dict) else None
        if key not in known:  # Part of a file name, so never taken from the server unchecked.
            continue
        present.add(key)
        path = folder / f"{key}.jpg"
        sha = str(row.get("sha256") or "").lower()
        if not (sha and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha):
            try:
                data = hrm_sync.angle_image(config, employee_id, key, opener=opener)
            except hrm_sync.HRMError as exc:
                problems.append(f"{key}: {exc}")
                continue
            if not data.startswith(b"\xff\xd8") or (sha and hashlib.sha256(data).hexdigest() != sha):
                problems.append(f"{key}: HRM sent a damaged image.")
                continue
            _write_private(path, data)
        items.append({"key": key, "path": path, "version": row.get("version")})
    for key in known - present:  # Deleted in HRM, so don't keep showing or storing it here.
        (folder / f"{key}.jpg").unlink(missing_ok=True)
    return items, problems


def cached_photo(settings, employee_id):
    """The cached HRM photo for this employee, or None."""
    name = _photo_name(employee_id)
    path = photo_dir(settings) / name if name else None
    return path if path is not None and path.is_file() else None


def _photo_name(employee_id):
    code = str(employee_id or "").strip()
    return hashlib.sha1(code.encode("utf-8")).hexdigest()[:20] + ".img" if code else ""


def fetch_photo(config, settings, employee_id, opener=None):
    """Download and cache one HRM photo. Returns the cached path, or None if HRM has none."""
    data = hrm_sync.employee_photo(config, employee_id, opener=opener)
    if not data:
        return None
    if not (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG") or data[8:12] == b"WEBP"):
        return None  # Not an image; never cache whatever came back.
    target = photo_dir(settings) / _photo_name(employee_id)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        target.parent.chmod(0o700)  # Employee photos; an older version left this folder readable.
    except OSError:
        pass
    _write_private(target, data)
    return target
