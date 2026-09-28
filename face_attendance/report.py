"""Read-only attendance reporting for the dashboard.

The persistence worker owns the only writing connection. The dashboard opens
its own connection with ``PRAGMA query_only=ON``, so a report can never record,
migrate or repair anything. Missing tables/columns (an unmigrated legacy file,
or a database the engine has not opened yet) degrade to empty results instead
of an exception.
"""
import csv
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path


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
    """Opt-in reporting label; it is computed on the fly and never persisted."""
    if not epoch or not work_start:
        return ""
    try:
        hour, minute = (int(part) for part in str(work_start).split(":"))
        started = datetime.fromtimestamp(float(epoch))
    except (OverflowError, OSError, ValueError):
        return ""
    return "LATE" if (started.hour, started.minute) > (hour, minute) else "ON TIME"


class AttendanceReader:
    def __init__(self, path):
        self.path = Path(path)
        self.error = ""
        self.connection = None
        if not self.path.exists():
            return
        try:
            self.connection = sqlite3.connect(str(self.path), timeout=1.5)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
        except sqlite3.Error as exc:
            self.error = str(exc)
            self.connection = None

    @property
    def available(self):
        return self.connection is not None

    def _columns(self):
        if self.connection is None:
            return set()
        try:
            return {row["name"] for row in self.connection.execute("PRAGMA table_info(attendance)")}
        except sqlite3.Error:
            return set()

    def _time_expression(self):
        columns = self._columns()
        parts = [name for name in ("recorded_at_epoch", "captured_at_epoch") if name in columns]
        parts.append("0")
        return f"COALESCE({', '.join(parts)})"

    def _filters(self, start=None, end=None, employee_id=None, search=None):
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
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params, time_column, employee_column

    def _rows(self, sql, params):
        if self.connection is None:
            return []
        try:
            return self.connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            self.error = str(exc)
            return []

    def _one(self, sql, params):
        rows = self._rows(sql, params)
        return rows[0] if rows else None


    def records(self, start=None, end=None, employee_id=None, search=None,
                limit=100, offset=0):
        where, params, time_column, _ = self._filters(start, end, employee_id, search)
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

    def count(self, start=None, end=None, employee_id=None, search=None):
        where, params, _, _ = self._filters(start, end, employee_id, search)
        row = self._one(f"SELECT COUNT(*) AS total FROM attendance{where}", params)
        return int(row["total"]) if row else 0

    def summary(self, start=None, end=None, employee_id=None, search=None):
        where, params, time_column, _ = self._filters(start, end, employee_id, search)
        row = self._one(f"""SELECT COUNT(*) AS records,
            COUNT(DISTINCT employee_id) AS employees,
            MIN(CASE WHEN {time_column} > 0 THEN {time_column} END) AS first_epoch,
            MAX({time_column}) AS last_epoch, SUM(COALESCE(duration, 0)) AS duration,
            COUNT(DISTINCT CASE WHEN {time_column} > 0
                THEN date({time_column}, 'unixepoch', 'localtime') END) AS days
            FROM attendance{where}""", params)
        if not row:
            return {"records": 0, "employees": 0, "first": None, "last": None,
                    "duration": 0.0, "days": 0}
        return {"records": int(row["records"]), "employees": int(row["employees"]),
                "first": row["first_epoch"], "last": row["last_epoch"],
                "duration": float(row["duration"] or 0), "days": int(row["days"])}

    def daily(self, start=None, end=None, employee_id=None, search=None, limit=60):
        where, params, time_column, _ = self._filters(start, end, employee_id, search)
        joiner = " AND " if where else " WHERE "
        rows = self._rows(f"""SELECT date({time_column}, 'unixepoch', 'localtime') AS day,
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
        counts = {"pending": 0, "synced": 0}
        for key, clause in (("pending", "synced_at IS NULL"), ("synced", "synced_at IS NOT NULL")):
            row = self._one(f"SELECT COUNT(*) AS total FROM attendance_outbox WHERE {clause}", [])
            counts[key] = int(row["total"]) if row else 0
        return counts

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def delete_records(db_path, record_ids, delete_snapshots=True, progress_cb=None):
    """Delete attendance rows by id and optionally their snapshot files.

    *progress_cb(done, total)* is called after each row so the UI can update a
    progress bar.  Returns ``(deleted_rows, deleted_files)``.
    """
    db_path = Path(db_path)
    if not db_path.exists() or not record_ids:
        return 0, 0
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    total = len(record_ids)
    deleted_rows = 0
    deleted_files = 0
    try:
        for i, rid in enumerate(record_ids):
            row = conn.execute(
                "SELECT snapshot, event_id FROM attendance WHERE id = ?", (rid,)).fetchone()
            if row:
                if delete_snapshots and row["snapshot"]:
                    snap = Path(row["snapshot"])
                    if snap.is_file():
                        try:
                            snap.unlink()
                            deleted_files += 1
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
    except sqlite3.Error:
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