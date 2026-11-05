#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  SV Face Recognition — PostgreSQL Remote Database Setup
#  Run this on your VPS (Ubuntu/Debian)
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

DB_NAME="${DB_NAME:-sv_attendance}"
DB_USER="${DB_USER:-sv_face}"
DB_PASS="${DB_PASS:-CHANGE_ME_NOW}"
ALLOW_IP="${ALLOW_IP:-0.0.0.0/0}"   # restrict to your client IP in production

echo "════════════════════════════════════════════════════════════════"
echo "  PostgreSQL Setup for SV Face Recognition"
echo "════════════════════════════════════════════════════════════════"

# ── 1. Install PostgreSQL ─────────────────────────────────────────────────
echo "[1/6] Installing PostgreSQL..."
sudo apt-get update -qq
sudo apt-get install -y -qq postgresql postgresql-contrib

# ── 2. Create database and user ──────────────────────────────────────────
echo "[2/6] Creating database '${DB_NAME}' and user '${DB_USER}'..."
sudo -u postgres psql <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
        CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}';
    END IF;
END
\$\$;

SELECT 'CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${DB_NAME}')
\gexec

GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};
SQL

# ── 3. Create schema ────────────────────────────────────────────────────
echo "[3/6] Creating tables..."
sudo -u postgres psql -d "${DB_NAME}" <<'SQL'
CREATE TABLE IF NOT EXISTS attendance (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    status          TEXT NOT NULL,
    snapshot        TEXT,
    duration        DOUBLE PRECISION,
    employee_id     TEXT,
    event_id        TEXT,
    captured_at_epoch  DOUBLE PRECISION,
    recorded_at_epoch  DOUBLE PRECISION
);

CREATE UNIQUE INDEX IF NOT EXISTS attendance_event_id
    ON attendance(event_id);
CREATE INDEX IF NOT EXISTS attendance_recorded_time
    ON attendance(recorded_at_epoch);
CREATE INDEX IF NOT EXISTS attendance_employee_time
    ON attendance(employee_id, recorded_at_epoch);

CREATE TABLE IF NOT EXISTS attendance_cooldowns (
    employee_id        TEXT PRIMARY KEY,
    last_capture_epoch DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance_outbox (
    event_id   TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    synced_at  TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    action    TEXT NOT NULL,
    detail    TEXT,
    actor     TEXT DEFAULT 'system'
);

-- Grant permissions
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO sv_face;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO sv_face;
SQL

# ── 4. Configure remote access ──────────────────────────────────────────
echo "[4/6] Configuring remote access..."
PG_VERSION=$(pg_config --version | grep -oP '\d+' | head -1)
PG_CONF="/etc/postgresql/${PG_VERSION}/main"

# Listen on all interfaces
sudo sed -i "s/#listen_addresses = 'localhost'/listen_addresses = '*'/" "${PG_CONF}/postgresql.conf"

# Allow remote connections (password auth)
if ! grep -q "${DB_NAME}" "${PG_CONF}/pg_hba.conf" 2>/dev/null; then
    echo "host  ${DB_NAME}  ${DB_USER}  ${ALLOW_IP}  scram-sha-256" | sudo tee -a "${PG_CONF}/pg_hba.conf"
fi

# ── 5. Firewall ─────────────────────────────────────────────────────────
echo "[5/6] Opening firewall port 5432..."
if command -v ufw &>/dev/null; then
    sudo ufw allow 5432/tcp
elif command -v firewall-cmd &>/dev/null; then
    sudo firewall-cmd --permanent --add-port=5432/tcp
    sudo firewall-cmd --reload
fi

# ── 6. Restart ──────────────────────────────────────────────────────────
echo "[6/6] Restarting PostgreSQL..."
sudo systemctl restart postgresql
sudo systemctl enable postgresql

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  DONE! PostgreSQL is ready for remote connections."
echo ""
echo "  Connection details for your app's settings.json:"
echo ""
echo "    \"db_backend\":  \"postgresql\","
echo "    \"db_host\":     \"YOUR_VPS_IP\","
echo "    \"db_port\":     5432,"
echo "    \"db_user\":     \"${DB_USER}\","
echo "    \"db_password\": \"${DB_PASS}\","
echo "    \"db_name\":     \"${DB_NAME}\""
echo ""
echo "  Python package to install on your client machine:"
echo "    pip install psycopg2-binary"
echo ""
echo "  Test connection from your local machine:"
echo "    psql -h YOUR_VPS_IP -U ${DB_USER} -d ${DB_NAME}"
echo ""
echo "  SECURITY REMINDERS:"
echo "    1. Change DB_PASS to a strong password"
echo "    2. Set ALLOW_IP to your client IP (not 0.0.0.0/0)"
echo "    3. Consider enabling SSL (see PostgreSQL docs)"
echo "════════════════════════════════════════════════════════════════"
