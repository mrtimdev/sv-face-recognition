Everything is done. Here's the full summary:

---

## What changed

### New files
- **[database.py](face_attendance/database.py)** — Database abstraction layer supporting SQLite, PostgreSQL, and MySQL. Handles placeholder translation (`?` → `%s`), dict-like row wrapping, schema introspection, and SQL dialect differences (upsert, date functions, auto-increment).
- **[vps_setup_postgresql.sh](scripts/vps_setup_postgresql.sh)** — One-command VPS setup: installs PG, creates user/database/schema, opens firewall, configures remote access.
- **[vps_setup_mysql.sh](scripts/vps_setup_mysql.sh)** — Same for MySQL.

### Modified files
- **[config.py](face_attendance/config.py)** / **[settings.py](face_attendance/settings.py)** — Added `db_backend`, `db_host`, `db_port`, `db_user`, `db_password`, `db_name`, `db_sslmode` fields.
- **[repository.py](face_attendance/repository.py)** — Uses `database.connect(config)` instead of raw `sqlite3.connect()`. Migration is backend-aware.
- **[report.py](face_attendance/report.py)** — `AttendanceReader` and `delete_records` use the database abstraction. Date queries are backend-aware.
- **[persistence.py](face_attendance/persistence.py)** — Passes full `config` to repository (not just `db_path`).
- **[splash.py](face_attendance/dashboard/splash.py)** — Database health check uses `database.connect()`.
- **Dashboard screens** — Pass `self.settings` instead of `self.settings.db_path` to reader/delete.

### How to use

**On your VPS** (Ubuntu/Debian):
```bash
# PostgreSQL:
DB_PASS="your_strong_password" bash scripts/vps_setup_postgresql.sh

# MySQL:
DB_PASS="your_strong_password" bash scripts/vps_setup_mysql.sh
```

**On your local machine**, install the driver:
```bash
pip install psycopg2-binary    # for PostgreSQL
pip install mysql-connector-python  # for MySQL
```

**In your `settings.json`** (example for PostgreSQL):
```json
{
  "db_backend": "postgresql",
  "db_host": "YOUR_VPS_IP",
  "db_port": 5432,
  "db_user": "sv_face",
  "db_password": "your_strong_password",
  "db_name": "sv_attendance"
}
```

All 237 tests pass, and existing SQLite setups (`"db_backend": "sqlite"`, the default) work unchanged.