"""First-run setup: choose the database and create the first administrator.

Qt-free so every step can be tested on its own. The wizard in
``dashboard/setup_wizard.py`` only collects answers and shows progress.
"""
import logging
import re
import secrets
import socket
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

from .config import ROOT
from .repository import AttendanceRepository
from .settings import SETTINGS_PATH
from .users import DEFAULT_ADMIN_PASSWORD, UserRepository

LOCAL_HOST = "127.0.0.1"
MYSQL_PORT = 3306
DEFAULT_DATABASE = "sv_attendance"
APP_DB_USER = "sv_face"
MIN_PASSWORD_LENGTH = 6
SETUP_MARKER = ".setup_complete"
_DATABASE_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")
_ACCESS_DENIED, _CANNOT_CONNECT = 1045, 2003
_MYSQL_DOWNLOADS = {"win32": "https://dev.mysql.com/downloads/installer/",
                    "darwin": "https://dev.mysql.com/downloads/mysql/"}
_MYSQL_INSTALL_COMMANDS = {"win32": "winget install Oracle.MySQL",
                           "darwin": "brew install mysql && brew services start mysql"}


class SetupError(Exception):
    """A setup step failed; the message is safe to show to the person installing."""


def _has_local_data(path):
    """An empty database is not earlier data: the splash check creates one."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            return any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                       for table in ("users", "attendance") if table in tables)
        finally:
            connection.close()
    except sqlite3.Error:
        return True  # Unreadable is never treated as a first run.


def setup_pending(frozen=None, root=None):
    """True until this released install has been through the wizard once."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    return bool(frozen) and not Path(root or ROOT, SETUP_MARKER).exists()


def mark_setup_complete(root=None):
    try:
        path = Path(root or ROOT, SETUP_MARKER)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        logging.exception("Could not record that setup finished")


def needs_setup(settings_path=None, frozen=None, root=None):
    """A brand-new install: no accounts to protect, so the wizard runs before sign-in."""
    if not setup_pending(frozen, root) or Path(settings_path or SETTINGS_PATH).exists():
        return False
    return not _has_local_data(Path(root or ROOT, "attendance.db").resolve())


def offer_after_sign_in(user, has_accounts, forced=False, frozen=None, root=None):
    """An existing install sees the wizard once, only after someone allowed to change settings
    signs in, so it can never be used to switch database or add an administrator without an account."""
    if not (forced or setup_pending(frozen, root)):
        return False
    if not has_accounts:
        return True
    return user is not None and user.has_permission("manage_settings")


def mysql_download_url(platform=None):
    return _MYSQL_DOWNLOADS.get(platform or sys.platform, "https://dev.mysql.com/downloads/mysql/")


def mysql_install_command(platform=None):
    return _MYSQL_INSTALL_COMMANDS.get(platform or sys.platform, "sudo apt install mysql-server")


def mysql_running(port=MYSQL_PORT, host=LOCAL_HOST, timeout=1.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _connect_as_admin(admin_user, admin_password, port):
    import mysql.connector
    try:
        return mysql.connector.connect(host=LOCAL_HOST, port=int(port), user=admin_user,
                                       password=admin_password, connection_timeout=5,
                                       use_pure=True)
    except mysql.connector.Error as exc:
        if exc.errno == _ACCESS_DENIED:
            raise SetupError("MySQL rejected that username or password.") from exc
        if exc.errno == _CANNOT_CONNECT:
            raise SetupError(f"MySQL is not running on port {port}.") from exc
        raise SetupError(f"Could not connect to MySQL: {exc.msg}") from exc


def provision_local_mysql(settings, admin_user, admin_password, port=MYSQL_PORT,
                          database=DEFAULT_DATABASE, on_step=None):
    """Create the database, a private app user and the tables on this computer.

    The admin password is used once and never stored. Returns the settings
    values to save and whether the database already holds accounts.
    """
    import mysql.connector
    step = on_step or (lambda _text: None)
    database = str(database or "").strip()
    if not _DATABASE_NAME.fullmatch(database):
        raise SetupError("Use only letters, numbers and underscores in the database name.")
    if not str(admin_user or "").strip():
        raise SetupError("Enter the MySQL administrator username.")

    step("Connecting to MySQL")
    connection = _connect_as_admin(admin_user.strip(), admin_password, port)
    # Random, but always satisfies MySQL's default password-strength rules.
    app_password = "Sv1!" + secrets.token_urlsafe(24)
    try:
        cursor = connection.cursor()
        step("Creating the database")
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` "
                       "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        step("Creating a private database user")
        # Both names: 127.0.0.1 only matches 'localhost' when name lookup is on.
        for host in ("localhost", LOCAL_HOST):
            cursor.execute("CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                           (APP_DB_USER, host, app_password))
            cursor.execute("ALTER USER %s@%s IDENTIFIED BY %s", (APP_DB_USER, host, app_password))
            cursor.execute(f"GRANT ALL PRIVILEGES ON `{database}`.* TO %s@%s", (APP_DB_USER, host))
        cursor.execute("FLUSH PRIVILEGES")
        connection.commit()
    except mysql.connector.Error as exc:
        raise SetupError(f"MySQL could not finish the setup: {exc.msg}") from exc
    finally:
        connection.close()

    values = dict(db_backend="mysql", db_host=LOCAL_HOST, db_port=int(port), db_user=APP_DB_USER,
                  db_password=app_password, db_name=database, db_sslmode="")
    candidate = replace(settings, **values)
    step("Creating the tables")
    try:
        AttendanceRepository(candidate).close()
        has_accounts = UserRepository(candidate).has_users()
    except Exception as exc:
        raise SetupError(f"The database was created, but its tables could not be: {exc}") from exc
    return values, has_accounts


def create_administrator(settings, username, password=""):
    """Create the first administrator; an empty password means the release default."""
    username = str(username or "").strip()
    if not username or len(username) > 100 or any(ord(char) < 32 for char in username):
        raise SetupError("Enter a username of at most 100 characters.")
    if password and len(password) < MIN_PASSWORD_LENGTH:
        raise SetupError(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.")
    return UserRepository(settings).create_user(username, "Administrator", "", "",
                                                password or DEFAULT_ADMIN_PASSWORD, role="admin")
