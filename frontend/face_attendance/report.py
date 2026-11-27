"""Read-only attendance reporting for the dashboard.

Supports SQLite, PostgreSQL, and MySQL via the ``database`` abstraction.
The dashboard opens a read-only connection so a report can never record,
migrate or repair anything.  Missing tables/columns degrade to empty results.
"""
import csv
import json
import shutil
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .database import connect, DatabaseError
from .storage import evidence_files


HEADERS = ("Recorded at (local)", "Employee", "Employee ID", "Status",
           "Verified presence (s)", "Snapshot", "Event ID")


def local_text(epoch, pattern="%Y-%m-%d %H:%M:%S"):
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch)).strftime(pattern)
    except (OverflowError, OSError, ValueError):
        return ""


def day_start(day=None):
    """Epoch of local midnight; attendance epochs are UTC, labels are local."""
    return datetime.combine(day or date.today(), time.min).timestamp()


PRESETS = ("today", "hour", "week", "month", "all")


def preset_range(preset, now=None):
    """(start, end) epochs for a named range; ``end=None`` means "still open"."""
    moment = datetime.fromtimestamp(now) if now else datetime.now()
    day = moment.date()
    if preset == "hour":
        return moment.timestamp() - 3600, None
    if preset == "today":
        return day_start(day), None
    if preset == "week":
        return day_start(day - timedelta(days=6)), None
    if preset == "month":
        return day_start(day.replace(day=1)), None
    return None, None


def punctuality(epoch, work_start):
    """Opt-in reporting label; computed on the fly, never persisted."""
    if not epoch or not work_start:
        return ""
    try:
        hour, minute = (int(part) for part in str(work_start).split(":"))
        started = datetime.fromtimestamp(float(epoch))
    except (OverflowError, OSError, ValueError):
        return ""
    return "LATE" if (started.hour, started.minute) > (hour, minute) else "ON TIME"


# Report status filter: (key, label). Unknown and Inactive are face observations saved by the
# Live Monitor (no attendance); Not enrolled are check-ins by someone HRM marks not face enrolled.
STATUS_FILTERS = (("all", "All status"), ("known", "Known"), ("not_enrolled", "Not enrolled"),
                  ("unknown", "Unknown"), ("inactive", "Inactive (blocked)"))
OBSERVATION_FOLDERS = {"unknown": "unknown", "inactive": "inactive"}
OBSERVATION_STATUS = {"unknown": "UNKNOWN", "inactive": "INACTIVE"}
NOT_ENROLLED = "NOT ENROLLED"


def _epoch_of(stamp):
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def observation_rows(capture_dir, kinds=("unknown", "inactive"), start=None, end=None, search=None):
    """Unknown and inactive faces the Live Monitor saved, newest first, shaped like report rows."""
    pattern = str(search or "").strip().casefold()
    rows = []
    for kind in kinds:
        folder = Path(capture_dir) / OBSERVATION_FOLDERS[kind]
        if not folder.is_dir():
            continue
        for entry in folder.iterdir():
            if entry.name.startswith(".") or not (entry / "record.json").is_file():
                continue
            try:
                info = json.loads((entry / "record.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(info, dict):
                continue
            epoch = _epoch_of(info.get("captured_at"))
            if (start is not None and epoch < start) or (end is not None and epoch >= end):
                continue
            name = str(info.get("employee_name") or "") or "Unknown face"
            employee_id = str(info.get("employee_id") or "")
            if pattern and pattern not in name.casefold() and pattern not in employee_id.casefold():
                continue
            rows.append({"id": None, "observation_dir": str(entry), "epoch": epoch,
                         "time": local_text(epoch), "event_id": str(info.get("event_id") or entry.name),
                         "name": name, "employee_id": employee_id, "status": OBSERVATION_STATUS[kind],
                         "snapshot": str(info.get("snapshot") or entry / "face.jpg"),
                         "duration": float(info.get("age") or 0), "timestamp": str(info.get("captured_at") or "")})
    rows.sort(key=lambda row: row["epoch"], reverse=True)
    return rows


def delete_observations(capture_dir, rows):
    """Remove saved observation folders; only ones inside *capture_dir*. Returns how many."""
    root = Path(capture_dir).resolve()
    deleted = 0
    for row in rows:
        folder = Path(row.get("observation_dir") or "")
        try:
            resolved = folder.resolve()
        except OSError:
            continue
        if resolved.parent.parent != root or not resolved.is_dir():
            continue
        shutil.rmtree(resolved, ignore_errors=True)
        deleted += not resolved.exists()
    return deleted


def _observation_summary(rows):
    return {"records": len(rows), "employees": len({r["employee_id"] for r in rows if r["employee_id"]}),
            "first": min((r["epoch"] for r in rows), default=None),
            "last": max((r["epoch"] for r in rows), default=None),
            "duration": sum(r["duration"] for r in rows),
            "days": len({local_text(r["epoch"], "%Y-%m-%d") for r in rows if r["epoch"]})}


def report_page(reader, capture_dir, status="all", not_enrolled_ids=(), start=None, end=None,
                search=None, limit=100, offset=0):
    """One page of the report for a STATUS_FILTERS key: (rows, total, summary).

    Check-ins by someone in *not_enrolled_ids* show as NOT ENROLLED. "all" mixes check-ins
    with saved observations, newest first.
    """
    ids = tuple(sorted({str(code).strip().casefold() for code in not_enrolled_ids if str(code).strip()}))
    limit, offset = max(1, int(limit)), max(0, int(offset))

    def mark(rows):
        for row in rows:
            if row["employee_id"].strip().casefold() in ids:
                row["status"] = NOT_ENROLLED
        return rows

    if status in OBSERVATION_FOLDERS:
        rows = observation_rows(capture_dir, (status,), start, end, search)
        return rows[offset:offset + limit], len(rows), _observation_summary(rows)
    if status in ("known", "not_enrolled"):
        if status == "not_enrolled" and not ids:
            return [], 0, _observation_summary([])
        scope = {"exclude_ids": ids} if status == "known" else {"include_ids": ids}
        rows = mark(reader.records(start=start, end=end, search=search, limit=limit, offset=offset, **scope))
        return (rows, reader.count(start=start, end=end, search=search, **scope),
                reader.summary(start=start, end=end, search=search, **scope))
    observed = observation_rows(capture_dir, tuple(OBSERVATION_FOLDERS), start, end, search)
    checkins = mark(reader.records(start=start, end=end, search=search, limit=offset + limit, offset=0))
    merged = sorted(checkins + observed, key=lambda row: row["epoch"], reverse=True)
    summary = reader.summary(start=start, end=end, search=search)
    return (merged[offset:offset + limit], reader.count(start=start, end=end, search=search) + len(observed),
            summary)


class AttendanceReader:
    def __init__(self, settings):
        self.error = ""
        self.connection = None
        self._col_cache = None
        self._time_expr_cache = None
        self._date_expr_cache = None
        if isinstance(settings, (str, Path)):
            backend = "sqlite"
            if not Path(str(settings)).exists():
                return
        else:
            backend = getattr(settings, "db_backend", "sqlite")
            if backend == "sqlite":
                path = Path(str(getattr(settings, "db_path", "attendance.db")))
                if not path.exists():
                    return
        try:
            self.connection = connect(settings, readonly=True)
        except DatabaseError as exc:
            self.error = str(exc)
            self.connection = None

    @property
    def available(self):
        return self.connection is not None

    def _columns(self):
        if self._col_cache is not None:
            return self._col_cache
        if self.connection is None:
            return set()
        try:
            self._col_cache = self.connection.get_columns("attendance")
            return self._col_cache
        except DatabaseError:
            return set()

    def _time_expression(self):
        if self._time_expr_cache is not None:
            return self._time_expr_cache
        columns = self._columns()
        parts = [name for name in ("recorded_at_epoch", "captured_at_epoch") if name in columns]
        parts.append("0")
        self._time_expr_cache = f"COALESCE({', '.join(parts)})"
        return self._time_expr_cache

    def _date_expression(self, time_column):
        if self._date_expr_cache is not None:
            return self._date_expr_cache
        if self.connection is not None:
            self._date_expr_cache = self.connection.date_from_epoch(time_column)
        else:
            self._date_expr_cache = f"date({time_column}, 'unixepoch', 'localtime')"
        return self._date_expr_cache

    def _filters(self, start=None, end=None, employee_id=None, search=None, include_ids=None, exclude_ids=None):
        time_column = self._time_expression()
        employee_column = "COALESCE(employee_id, '')" if "employee_id" in self._columns() else "''"
        clauses, params = [], []
        if start is not None:
            clauses.append(f"{time_column} >= ?")
            params.append(float(start))
        if end is not None:
            clauses.append(f"{time_column} < ?")
            params.append(float(end))
        if employee_id:
            clauses.append(f"{employee_column} = ?")
            params.append(str(employee_id))
        if search:
            pattern = f"%{str(search).strip()}%"
            clauses.append(f"(name LIKE ? OR {employee_column} LIKE ?)")
            params += [pattern, pattern]
        for ids, operator in ((include_ids, "IN"), (exclude_ids, "NOT IN")):
            ids = [str(code).strip().casefold() for code in ids or ()]
            if ids:
                clauses.append(f"LOWER(TRIM({employee_column})) {operator} ({', '.join('?' * len(ids))})")
                params += ids
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params, time_column, employee_column

    def _rows(self, sql, params):
        if self.connection is None:
            return []
        try:
            return self.connection.execute(sql, params).fetchall()
        except DatabaseError as exc:
            self.error = str(exc)
            return []

    def _one(self, sql, params):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def records(self, start=None, end=None, employee_id=None, search=None,
                limit=100, offset=0, include_ids=None, exclude_ids=None):
        where, params, time_column, _ = self._filters(start, end, employee_id, search, include_ids, exclude_ids)
        event_column = "event_id" if "event_id" in self._columns() else "''"
        rows = self._rows(f"""SELECT id, {time_column} AS epoch, {event_column} AS event_id,
            COALESCE(name, '') AS name, COALESCE(employee_id, '') AS employee_id,
            COALESCE(status, '') AS status, COALESCE(snapshot, '') AS snapshot,
            COALESCE(duration, 0) AS duration, COALESCE(timestamp, '') AS timestamp
            FROM attendance{where} ORDER BY epoch DESC, id DESC LIMIT ? OFFSET ?""",
                          params + [max(1, int(limit)), max(0, int(offset))])
        return [{"id": row["id"], "epoch": float(row["epoch"] or 0),
                 "time": local_text(row["epoch"]), "event_id": row["event_id"],
                 "name": row["name"], "employee_id": row["employee_id"],
                 "status": row["status"], "snapshot": row["snapshot"],
                 "duration": float(row["duration"] or 0), "timestamp": row["timestamp"]}
                for row in rows]

    def count(self, start=None, end=None, employee_id=None, search=None, include_ids=None, exclude_ids=None):
        where, params, _, _ = self._filters(start, end, employee_id, search, include_ids, exclude_ids)
        row = self._one(f"SELECT COUNT(*) AS total FROM attendance{where}", params)
        return int(row["total"]) if row else 0

    def summary(self, start=None, end=None, employee_id=None, search=None, include_ids=None, exclude_ids=None):
        where, params, time_column, _ = self._filters(start, end, employee_id, search, include_ids, exclude_ids)
        date_expr = self._date_expression(time_column)
        row = self._one(f"""SELECT COUNT(*) AS records,
            COUNT(DISTINCT employee_id) AS employees,
            MIN(CASE WHEN {time_column} > 0 THEN {time_column} END) AS first_epoch,
            MAX({time_column}) AS last_epoch, SUM(COALESCE(duration, 0)) AS duration,
            COUNT(DISTINCT CASE WHEN {time_column} > 0
                THEN {date_expr} END) AS days
            FROM attendance{where}""", params)
        if not row:
            return {"records": 0, "employees": 0, "first": None, "last": None,
                    "duration": 0.0, "days": 0}
        return {"records": int(row["records"]), "employees": int(row["employees"]),
                "first": row["first_epoch"], "last": row["last_epoch"],
                "duration": float(row["duration"] or 0), "days": int(row["days"])}

    def daily(self, start=None, end=None, employee_id=None, search=None, limit=60):
        where, params, time_column, _ = self._filters(start, end, employee_id, search)
        date_expr = self._date_expression(time_column)
        joiner = " AND " if where else " WHERE "
        rows = self._rows(f"""SELECT {date_expr} AS day,
            COUNT(*) AS records, COUNT(DISTINCT employee_id) AS employees,
            MIN({time_column}) AS first_epoch, MAX({time_column}) AS last_epoch
            FROM attendance{where}{joiner}{time_column} > 0
            GROUP BY day ORDER BY day DESC LIMIT ?""", params + [max(1, int(limit))])
        return [{"day": row["day"], "records": int(row["records"]),
                 "employees": int(row["employees"]),
                 "first": local_text(row["first_epoch"], "%H:%M:%S"),
                 "last": local_text(row["last_epoch"], "%H:%M:%S")} for row in rows]

    def employee_totals(self, start=None, end=None, search=None, limit=200):
        where, params, time_column, employee_column = self._filters(start, end, None, search)
        rows = self._rows(f"""SELECT {employee_column} AS employee_id, MAX(name) AS name,
            COUNT(*) AS records, MIN({time_column}) AS first_epoch, MAX({time_column}) AS last_epoch
            FROM attendance{where} GROUP BY employee_id ORDER BY records DESC, name LIMIT ?""",
                          params + [max(1, int(limit))])
        return [{"employee_id": row["employee_id"], "name": row["name"],
                 "records": int(row["records"]), "first": local_text(row["first_epoch"]),
                 "last": local_text(row["last_epoch"])} for row in rows]

    def outbox_counts(self):
        counts = {"pending": 0, "synced": 0, "rejected": 0, "unassigned": 0, "legacy_resolved": 0}
        if self.connection is None:
            return counts
        columns = self.connection.get_columns("attendance_outbox")
        if "delivery_status" not in columns:
            clauses = (("pending", "synced_at IS NULL"), ("unassigned", "synced_at IS NULL"),
                       ("legacy_resolved", "synced_at IS NOT NULL"))
        else:
            clauses = (("pending", "delivery_status='pending'"),
                       ("synced", "delivery_status='delivered'"),
                       ("rejected", "delivery_status='rejected'"),
                       ("legacy_resolved", "delivery_status='legacy_resolved'"),
                       ("unassigned", "delivery_status='pending' AND (destination IS NULL OR destination='')"))
        for key, clause in clauses:
            row = self._one(f"SELECT COUNT(*) AS total FROM attendance_outbox WHERE {clause}", [])
            counts[key] = int(row["total"]) if row else 0
        return counts

    def delivery_issues(self, limit=100):
        if self.connection is None or "delivery_status" not in self.connection.get_columns("attendance_outbox"):
            return []
        return [dict(row) for row in self.connection.execute(
            "SELECT event_id, destination, delivery_status, delivery_reason, created_at "
            "FROM attendance_outbox WHERE delivery_status IN ('rejected', 'pending', 'legacy_resolved') "
            "ORDER BY created_at LIMIT ?", (limit,))]

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def delete_records(settings, record_ids, delete_snapshots=True, progress_cb=None):
    """Delete attendance rows by id and optionally their snapshot files."""
    if not record_ids:
        return 0, 0
    if isinstance(settings, (str, Path)):
        if not Path(str(settings)).exists():
            return 0, 0
    else:
        backend = getattr(settings, "db_backend", "sqlite")
        if backend == "sqlite":
            path = Path(str(getattr(settings, "db_path", "attendance.db")))
            if not path.exists():
                return 0, 0
    conn = connect(settings)
    total = len(record_ids)
    deleted_rows = 0
    deleted_files = 0
    try:
        for i, rid in enumerate(record_ids):
            row = conn.execute(
                "SELECT snapshot, event_id FROM attendance WHERE id = ?", (rid,)).fetchone()
            if row:
                if delete_snapshots and row["snapshot"]:
                    for snap in evidence_files(row["snapshot"]):
                        try:
                            if snap.is_file():
                                snap.unlink()
                                deleted_files += snap.suffix.lower() in (".jpg", ".jpeg", ".png")
                        except OSError:
                            pass
                if row["event_id"]:
                    conn.execute("DELETE FROM attendance_outbox WHERE event_id = ?",
                                 (row["event_id"],))
            conn.execute("DELETE FROM attendance WHERE id = ?", (rid,))
            deleted_rows += 1
            if progress_cb:
                progress_cb(i + 1, total)
        conn.commit()
    except DatabaseError:
        conn.rollback()
        raise
    finally:
        conn.close()
    return deleted_rows, deleted_files


def export_rows(path, rows, work_start="", punctuality_column=False):
    """Write the filtered report as UTF-8 CSV; returns the number of data rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = list(HEADERS)
    if punctuality_column:
        headers.insert(3, "Punctuality")
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            values = [row["time"], row["name"], row["employee_id"]]
            if punctuality_column:
                values.append(punctuality(row["epoch"], work_start))
            values += [row["status"], f"{row['duration']:.2f}", row["snapshot"], row["event_id"]]
            writer.writerow(values)
    return len(rows)


def export_excel(path, rows, work_start="", punctuality_column=False):
    """Write the filtered report as an .xlsx workbook; returns the row count."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = list(HEADERS)
    if punctuality_column:
        headers.insert(3, "Punctuality")

    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance Report"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1E40AF", end_color="1E40AF", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")
    thin = Side(style="thin", color="E5EAF2")
    cell_border = Border(bottom=thin)

    for col, title in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    for r, row in enumerate(rows, 2):
        values = [row["time"], row["name"], row["employee_id"]]
        if punctuality_column:
            values.append(punctuality(row["epoch"], work_start))
        values += [row["status"], round(row["duration"], 2),
                   Path(row["snapshot"]).name if row["snapshot"] else "", row["event_id"]]
        for c, val in enumerate(values, 1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.border = cell_border
            if c in (5, 6) and isinstance(val, (int, float)):
                cell.alignment = Alignment(horizontal="right")

    for col in range(1, len(headers) + 1):
        letter = get_column_letter(col)
        max_len = max(len(str(ws.cell(row=r, column=col).value or ""))
                      for r in range(1, min(len(rows) + 2, 52)))
        ws.column_dimensions[letter].width = min(45, max(12, max_len + 4))

    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    ws.freeze_panes = "A2"
    wb.save(str(path))
    return len(rows)
