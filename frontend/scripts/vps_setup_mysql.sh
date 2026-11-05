#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  SV Face Recognition — MySQL Remote Database Setup
#  Run this on your VPS (Ubuntu/Debian)
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

DB_NAME="${DB_NAME:-sv_attendance}"
DB_USER="${DB_USER:-sv_face}"
DB_PASS="${DB_PASS:-CHANGE_ME_NOW}"
ALLOW_HOST="${ALLOW_HOST:-%}"   # restrict to your client IP in production

echo "════════════════════════════════════════════════════════════════"
echo "  MySQL Setup for SV Face Recognition"
echo "════════════════════════════════════════════════════════════════"

# ── 1. Install MySQL ────────────────────────────────────────────────────
echo "[1/6] Installing MySQL Server..."
sudo apt-get update -qq
sudo apt-get install -y -qq mysql-server

# ── 2. Secure installation basics ───────────────────────────────────────
echo "[2/6] Securing MySQL..."
sudo systemctl start mysql
sudo systemctl enable mysql

# ── 3. Create database and user ─────────────────────────────────────────
echo "[3/6] Creating database '${DB_NAME}' and user '${DB_USER}'..."
sudo mysql <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS '${DB_USER}'@'${ALLOW_HOST}'
    IDENTIFIED BY '${DB_PASS}';

GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'${ALLOW_HOST}';
FLUSH PRIVILEGES;
SQL

# ── 4. Create schema ───────────────────────────────────────────────────
echo "[4/6] Creating tables..."
sudo mysql "${DB_NAME}" <<'SQL'
CREATE TABLE IF NOT EXISTS attendance (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    name               VARCHAR(255) NOT NULL,
    timestamp          VARCHAR(255) NOT NULL,
    status             VARCHAR(50) NOT NULL,
    snapshot           TEXT,
    duration           DOUBLE,
    employee_id        VARCHAR(255),
    event_id           VARCHAR(255),
    captured_at_epoch  DOUBLE,
    recorded_at_epoch  DOUBLE,
    UNIQUE KEY idx_event_id (event_id),
    KEY idx_recorded_time (recorded_at_epoch),
    KEY idx_employee_time (employee_id, recorded_at_epoch)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS attendance_cooldowns (
    employee_id        VARCHAR(255) PRIMARY KEY,
    last_capture_epoch DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS attendance_outbox (
    event_id   VARCHAR(255) PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at VARCHAR(255) NOT NULL,
    synced_at  VARCHAR(255)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS audit_log (
    id        INT AUTO_INCREMENT PRIMARY KEY,
    timestamp VARCHAR(255) NOT NULL,
    action    VARCHAR(255) NOT NULL,
    detail    TEXT,
    actor     VARCHAR(255) DEFAULT 'system'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
SQL

# ── 5. Configure remote access ─────────────────────────────────────────
echo "[5/6] Configuring remote access..."
MYSQL_CONF="/etc/mysql/mysql.conf.d/mysqld.cnf"
if [ -f "$MYSQL_CONF" ]; then
    sudo sed -i "s/^bind-address\s*=.*/bind-address = 0.0.0.0/" "$MYSQL_CONF"
elif [ -f "/etc/mysql/my.cnf" ]; then
    MYSQL_CONF="/etc/mysql/my.cnf"
    sudo sed -i "s/^bind-address\s*=.*/bind-address = 0.0.0.0/" "$MYSQL_CONF"
fi

# ── 6. Firewall + restart ──────────────────────────────────────────────
echo "[6/6] Opening firewall port 3306 and restarting MySQL..."
if command -v ufw &>/dev/null; then
    sudo ufw allow 3306/tcp
elif command -v firewall-cmd &>/dev/null; then
    sudo firewall-cmd --permanent --add-port=3306/tcp
    sudo firewall-cmd --reload
fi

sudo systemctl restart mysql

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  DONE! MySQL is ready for remote connections."
echo ""
echo "  Connection details for your app's settings.json:"
echo ""
echo "    \"db_backend\":  \"mysql\","
echo "    \"db_host\":     \"YOUR_VPS_IP\","
echo "    \"db_port\":     3306,"
echo "    \"db_user\":     \"${DB_USER}\","
echo "    \"db_password\": \"${DB_PASS}\","
echo "    \"db_name\":     \"${DB_NAME}\""
echo ""
echo "  Python package to install on your client machine:"
echo "    pip install mysql-connector-python"
echo ""
echo "  Test connection from your local machine:"
echo "    mysql -h YOUR_VPS_IP -u ${DB_USER} -p ${DB_NAME}"
echo ""
echo "  SECURITY REMINDERS:"
echo "    1. Change DB_PASS to a strong password"
echo "    2. Set ALLOW_HOST to your client IP (not %)"
echo "    3. Run: sudo mysql_secure_installation"
echo "    4. Consider enabling SSL (see MySQL docs)"
echo "════════════════════════════════════════════════════════════════"
