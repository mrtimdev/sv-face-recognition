"""Database abstraction: SQLite (local), PostgreSQL, or MySQL (remote).

Provides ``connect()`` to create backend-appropriate connections.  The returned
``Connection`` wraps raw driver connections with a unified interface:

- Automatic ``?`` to ``%s`` placeholder translation for PG/MySQL
- Dict-like row access (integer index *and* key access)
- ``get_columns(table)`` for schema introspection
- ``date_from_epoch(col)`` for cross-backend date extraction
- ``begin_immediate()`` / ``checkpoint()`` that adapt to each engine
"""
import logging

log = logging.getLogger(__name__)

BACKENDS = ("sqlite", "postgresql", "mysql")


def _build_db_errors():
    errors = []
    try:
        import sqlite3
        errors.append(sqlite3.Error)
    except ImportError:
        pass
    try:
        import psycopg2
        errors.append(psycopg2.Error)
    except ImportError:
        pass
    try:
        from mysql.connector import errors as _me
        errors.append(_me.Error)
    except ImportError:
        pass
    return tuple(errors) if errors else (Exception,)


DatabaseError = _build_db_errors()


# ── Row wrapper ───────────────────────────────────────────────────────────

class Row:
    """Dict-like row that supports both ``row["key"]`` and ``row[0]`` access."""
    __slots__ = ("_map", "_keys", "_values")

    def __init__(self, mapping):
        self._map = {k.lower(): v for k, v in mapping.items()}
        self._keys = list(self._map.keys())
        self._values = list(self._map.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._map[key.lower()]

    def __contains__(self, key):
        return key.lower() in self._map

    def keys(self):
        return self._map.keys()

    def get(self, key, default=None):
        return self._map.get(key.lower(), default)


# ── Cursor wrapper ────────────────────────────────────────────────────────

class Cursor:
    """Wraps a raw DB-API cursor so every row comes back as a dict-like object."""

    def __init__(self, raw, backend):
        self._raw = raw
        self._backend = backend

    def fetchone(self):
        row = self._raw.fetchone()
        if row is None:
            return None
        if self._backend == "sqlite":
            return row
        return Row(row)

    def fetchall(self):
        rows = self._raw.fetchall()
        if self._backend == "sqlite":
            return rows
        return [Row(r) for r in rows]

    def __iter__(self):
        if self._backend == "sqlite":
            return iter(self._raw)
        return self

    def __next__(self):
        row = next(self._raw)
        return Row(row)

    @property
    def lastrowid(self):
        return self._raw.lastrowid

    @property
    def rowcount(self):
        return self._raw.rowcount


# ── Connection wrapper ────────────────────────────────────────────────────

class Connection:
    """Unified database connection.  Translates placeholders and provides
    backend-aware helpers for schema inspection and SQL dialect differences."""

    def __init__(self, raw, backend, db_name=""):
        self._raw = raw
        self.backend = backend
        self._db_name = db_name
        self._col_cache = {}
        self._table_cache = {}

    # ── execute / commit / close ──────────────────────────────────────────

    def execute(self, sql, params=None):
        sql = self._translate(sql)
        if self.backend == "sqlite":
            return Cursor(self._raw.execute(sql, params or ()), "sqlite")
        if self.backend == "postgresql":
            import psycopg2.extras
            cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = self._raw.cursor(dictionary=True, buffered=True)
        cur.execute(sql, params or ())
        return Cursor(cur, self.backend)

    def _translate(self, sql):
        if self.backend == "sqlite":
            return sql
        out, in_str, qc = [], False, None
        for ch in sql:
            if in_str:
                out.append(ch)
                if ch == qc:
                    in_str = False
            elif ch in ("'", '"'):
                in_str, qc = True, ch
                out.append(ch)
            elif ch == "?":
                out.append("%s")
            else:
                out.append(ch)
        return "".join(out)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass

    # ── context manager (auto commit / rollback) ──────────────────────────

    def __enter__(self):
        if self.backend == "sqlite":
            self._raw.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.backend == "sqlite":
            return self._raw.__exit__(exc_type, exc_val, exc_tb)
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()
        return False

    # ── transaction helpers ───────────────────────────────────────────────

    def begin_immediate(self):
        if self.backend == "sqlite":
            self._raw.execute("BEGIN IMMEDIATE")
        elif self.backend == "mysql":
            cur = self._raw.cursor()
            cur.execute("START TRANSACTION")
            cur.close()

    def checkpoint(self):
        if self.backend == "sqlite":
            try:
                self._raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                log.exception("WAL checkpoint failed")

    def integrity_check(self):
        if self.backend == "sqlite":
            self.execute("PRAGMA integrity_check").fetchone()
        else:
            self.execute("SELECT 1").fetchone()

    # ── schema introspection ──────────────────────────────────────────────

    def get_columns(self, table):
        if table in self._col_cache:
            return self._col_cache[table]
        if self.backend == "sqlite":
            cols = {r["name"] for r in self.execute(f"PRAGMA table_info({table})").fetchall()}
        elif self.backend == "postgresql":
            rows = self.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ?", (table,)).fetchall()
            cols = {r["column_name"] for r in rows}
        else:
            rows = self.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ?",
                (self._db_name, table)).fetchall()
            cols = {r["column_name"] for r in rows}
        self._col_cache[table] = cols
        return cols

    def table_exists(self, name):
        if name in self._table_cache:
            return self._table_cache[name]
        if self.backend == "sqlite":
            r = self.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        elif self.backend == "postgresql":
            r = self.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=?", (name,)).fetchone()
        else:
            r = self.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=? AND table_name=?",
                (self._db_name, name)).fetchone()
        self._table_cache[name] = r is not None
        return self._table_cache[name]

    # ── SQL dialect helpers ───────────────────────────────────────────────

    def date_from_epoch(self, col):
        if self.backend == "sqlite":
            return f"date({col}, 'unixepoch', 'localtime')"
        if self.backend == "postgresql":
            return f"to_char(to_timestamp({col}), 'YYYY-MM-DD')"
        return f"DATE(FROM_UNIXTIME({col}))"

    def auto_id_column(self):
        if self.backend == "sqlite":
            return "id INTEGER PRIMARY KEY AUTOINCREMENT"
        if self.backend == "postgresql":
            return "id SERIAL PRIMARY KEY"
        return "id INT AUTO_INCREMENT PRIMARY KEY"

    def real_type(self):
        if self.backend == "sqlite":
            return "REAL"
        if self.backend == "postgresql":
            return "DOUBLE PRECISION"
        return "DOUBLE"

    def upsert_cooldown(self, employee_id, epoch):
        if self.backend == "sqlite":
            self.execute(
                "INSERT INTO attendance_cooldowns VALUES (?, ?) "
                "ON CONFLICT(employee_id) DO UPDATE SET "
                "last_capture_epoch = MAX(last_capture_epoch, excluded.last_capture_epoch)",
                (employee_id, epoch))
        elif self.backend == "mysql":
            self.execute(
                "INSERT INTO attendance_cooldowns (employee_id, last_capture_epoch) "
                "VALUES (?, ?) ON DUPLICATE KEY UPDATE "
                "last_capture_epoch = GREATEST(last_capture_epoch, VALUES(last_capture_epoch))",
                (employee_id, epoch))
        else:
            self.execute(
                "INSERT INTO attendance_cooldowns VALUES (?, ?) "
                "ON CONFLICT(employee_id) DO UPDATE SET "
                "last_capture_epoch = GREATEST(last_capture_epoch, excluded.last_capture_epoch)",
                (employee_id, epoch))


# ── connection factory ────────────────────────────────────────────────────

def connect(settings, readonly=False):
    """Create a ``Connection`` for the configured backend.

    *settings* can be a ``Settings``, ``Config``, or plain ``Path``/``str``
    (treated as a local SQLite file for backward compatibility).
    """
    from pathlib import Path as _Path
    if isinstance(settings, (str, _Path)):
        backend = "sqlite"
        _sqlite_path = str(settings)
    else:
        backend = getattr(settings, "db_backend", "sqlite")
        _sqlite_path = str(getattr(settings, "db_path", "attendance.db"))

    if backend == "sqlite":
        import sqlite3
        path = _sqlite_path
        conn = sqlite3.connect(path, timeout=1.5)
        conn.row_factory = sqlite3.Row
        if readonly:
            conn.execute("PRAGMA query_only=ON")
        return Connection(conn, "sqlite")

    if backend == "postgresql":
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError(
                "PostgreSQL backend requires psycopg2.\n"
                "Install it with:  pip install psycopg2-binary") from None
        params = dict(
            host=getattr(settings, "db_host", "localhost"),
            port=int(getattr(settings, "db_port", 0) or 5432),
            user=getattr(settings, "db_user", ""),
            password=getattr(settings, "db_password", ""),
            dbname=getattr(settings, "db_name", "sv_attendance"),
        )
        sslmode = getattr(settings, "db_sslmode", "")
        if sslmode:
            params["sslmode"] = sslmode
        conn = psycopg2.connect(**params)
        if readonly:
            conn.set_session(readonly=True, autocommit=True)
        else:
            conn.autocommit = False
        return Connection(conn, "postgresql", params["dbname"])

    if backend == "mysql":
        try:
            import mysql.connector
        except ImportError:
            raise RuntimeError(
                "MySQL backend requires mysql-connector-python.\n"
                "Install it with:  pip install mysql-connector-python") from None
        params = dict(
            host=getattr(settings, "db_host", "localhost"),
            port=int(getattr(settings, "db_port", 0) or 3306),
            user=getattr(settings, "db_user", ""),
            password=getattr(settings, "db_password", ""),
            database=getattr(settings, "db_name", "sv_attendance"),
            autocommit=False,
        )
        ssl_ca = getattr(settings, "db_ssl_ca", "")
        if ssl_ca:
            params["ssl_ca"] = ssl_ca
        conn = mysql.connector.connect(**params)
        return Connection(conn, "mysql", params["database"])

    raise ValueError(f"Unknown db_backend: {backend!r}")
