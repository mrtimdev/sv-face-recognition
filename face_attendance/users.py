"""User accounts with role-based access (USER / ADMIN).

Passwords are hashed with PBKDF2-SHA256 + random salt.  The ``UserRepository``
handles CRUD against the ``users`` table through the ``database`` abstraction.
"""
import hashlib
import json
import os
import secrets
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT
from .database import connect, DatabaseError

ROLES = ("admin", "user")

ALL_PERMISSIONS = (
    "view_dashboard",
    "view_reports",
    "manage_employees",
    "manage_users",
    "manage_settings",
    "export_data",
    "control_engine",
)

ROLE_DEFAULTS = {
    "admin": list(ALL_PERMISSIONS),
    "user": ["view_dashboard", "view_reports"],
}

SESSION_PATH = ROOT / ".session"
# Present once this machine has seen user accounts, so an unreachable database
# can never silently turn sign-in off.
ACCOUNTS_MARKER = ROOT / ".accounts"


class AccountDisabledError(Exception):
    """Raised by ``authenticate`` when the password is right but the account is disabled."""

    def __init__(self, user):
        super().__init__(f"The account '{user.username}' is disabled.")
        self.user = user


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return salt.hex() + ":" + key.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
        return key.hex() == key_hex
    except (ValueError, AttributeError):
        return False


@dataclass
class User:
    uuid: str
    username: str
    full_name: str
    phone: str
    email: str
    password_hash: str
    role: str
    permissions: list = field(default_factory=list)
    avatar_path: str = ""
    created_at: str = ""
    updated_at: str = ""
    disabled: bool = False

    @property
    def active(self) -> bool:
        return not self.disabled

    def has_permission(self, perm: str) -> bool:
        return perm in self.permissions

    def has_any_permission(self, *perms: str) -> bool:
        return any(p in self.permissions for p in perms)


@dataclass
class Session:
    user_uuid: str
    username: str
    full_name: str
    role: str
    token: str
    created_at: str


def save_session(user: User) -> Session:
    token = secrets.token_hex(32)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session = Session(
        user_uuid=user.uuid, username=user.username,
        full_name=user.full_name, role=user.role,
        token=token, created_at=now,
    )
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps({
        "user_uuid": session.user_uuid,
        "username": session.username,
        "full_name": session.full_name,
        "role": session.role,
        "token": session.token,
        "created_at": session.created_at,
    }), encoding="utf-8")
    try:
        os.chmod(SESSION_PATH, 0o600)
    except OSError:
        pass
    return session


def load_session() -> Session | None:
    if not SESSION_PATH.exists():
        return None
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        return Session(
            user_uuid=data["user_uuid"], username=data["username"],
            full_name=data.get("full_name", ""), role=data.get("role", "user"),
            token=data["token"], created_at=data["created_at"],
        )
    except (json.JSONDecodeError, KeyError, OSError):
        clear_session()
        return None


def clear_session():
    SESSION_PATH.unlink(missing_ok=True)


def remember_accounts_exist():
    try:
        ACCOUNTS_MARKER.parent.mkdir(parents=True, exist_ok=True)
        ACCOUNTS_MARKER.touch(exist_ok=True)
    except OSError:
        pass


def accounts_expected() -> bool:
    """True when this machine has had user accounts (see ``ACCOUNTS_MARKER``)."""
    return ACCOUNTS_MARKER.exists()


class UserRepository:
    def __init__(self, settings):
        self._settings = settings

    def _connect(self):
        return connect(self._settings)

    def _ensure_table(self, conn):
        if conn.table_exists("users"):
            cols = {c.lower() for c in conn.get_columns("users")}
            text = "VARCHAR(255)" if conn.backend == "mysql" else "TEXT"
            migrations = [
                ("full_name", f"{text} NOT NULL DEFAULT ''"),
                ("avatar_path", "TEXT NOT NULL DEFAULT ''"),
                ("permissions", "TEXT NOT NULL DEFAULT '[]'"),
                ("disabled", "INTEGER NOT NULL DEFAULT 0"),
            ]
            for col, typedef in migrations:
                if col not in cols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
                    conn.commit()
            return
        text = "VARCHAR(255)" if conn.backend == "mysql" else "TEXT"
        pk = f"{text} PRIMARY KEY" if conn.backend == "mysql" else "TEXT PRIMARY KEY"
        conn.execute(f"""CREATE TABLE IF NOT EXISTS users (
            uuid {pk},
            username {text} NOT NULL,
            full_name {text} NOT NULL DEFAULT '',
            phone {text} NOT NULL DEFAULT '',
            email {text} NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            role {text} NOT NULL DEFAULT 'user',
            permissions TEXT NOT NULL DEFAULT '[]',
            avatar_path TEXT NOT NULL DEFAULT '',
            disabled INTEGER NOT NULL DEFAULT 0,
            created_at {text} NOT NULL,
            updated_at {text} NOT NULL)""")
        if conn.backend == "mysql":
            try:
                conn.execute("CREATE UNIQUE INDEX users_username ON users(username)")
            except DatabaseError:
                pass
        else:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS users_username ON users(username)")
        conn.commit()

    def has_users(self) -> bool:
        conn = self._connect()
        try:
            self._ensure_table(conn)
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"]) > 0
        finally:
            conn.close()

    def authenticate(self, username: str, password: str, on_stage=None):
        """Return the matching ``User`` or ``None``.

        A disabled account raises ``AccountDisabledError`` - but only after the
        password matched, so the check never reveals which accounts exist.
        *on_stage* (optional) is called with ``"verify"`` once the database has
        answered, before the deliberately slow password hash runs, so a sign-in
        screen can report real progress.
        """
        conn = self._connect()
        try:
            self._ensure_table(conn)
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if on_stage is not None:
                on_stage("verify")
            if row and verify_password(password, row["password_hash"]):
                user = self._to_user(row)
                if user.disabled:
                    raise AccountDisabledError(user)
                return user
            return None
        finally:
            conn.close()

    def create_user(self, username, full_name, phone, email, password,
                    role="user", permissions=None, avatar_path=""):
        uid = str(_uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pw_hash = hash_password(password)
        if permissions is None:
            permissions = ROLE_DEFAULTS.get(role, ROLE_DEFAULTS["user"])
        perms_json = json.dumps(permissions)
        conn = self._connect()
        try:
            self._ensure_table(conn)
            conn.execute(
                "INSERT INTO users (uuid, username, full_name, phone, email, "
                "password_hash, role, permissions, avatar_path, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, username, full_name, phone, email, pw_hash, role,
                 perms_json, avatar_path, now, now))
            conn.commit()
        finally:
            conn.close()
        return User(uuid=uid, username=username, full_name=full_name,
                    phone=phone, email=email, password_hash=pw_hash,
                    role=role, permissions=permissions,
                    avatar_path=avatar_path, created_at=now, updated_at=now)

    def update_user(self, uid, username=None, full_name=None, phone=None,
                    email=None, password=None, role=None, permissions=None,
                    avatar_path=None, disabled=None):
        conn = self._connect()
        try:
            self._ensure_table(conn)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            sets, params = ["updated_at = ?"], [now]
            if username is not None:
                sets.append("username = ?")
                params.append(username)
            if full_name is not None:
                sets.append("full_name = ?")
                params.append(full_name)
            if phone is not None:
                sets.append("phone = ?")
                params.append(phone)
            if email is not None:
                sets.append("email = ?")
                params.append(email)
            if password is not None:
                sets.append("password_hash = ?")
                params.append(hash_password(password))
            if role is not None:
                sets.append("role = ?")
                params.append(role)
            if permissions is not None:
                sets.append("permissions = ?")
                params.append(json.dumps(permissions))
            if avatar_path is not None:
                sets.append("avatar_path = ?")
                params.append(avatar_path)
            if disabled is not None:
                sets.append("disabled = ?")
                params.append(1 if disabled else 0)
            params.append(uid)
            conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE uuid = ?", params)
            conn.commit()
        finally:
            conn.close()

    def set_disabled(self, uid, disabled=True):
        """Block (or restore) sign-in while keeping the account and its history."""
        self.update_user(uid, disabled=bool(disabled))

    def delete_user(self, uid):
        conn = self._connect()
        try:
            self._ensure_table(conn)
            conn.execute("DELETE FROM users WHERE uuid = ?", (uid,))
            conn.commit()
        finally:
            conn.close()

    def get_user(self, uid):
        conn = self._connect()
        try:
            self._ensure_table(conn)
            row = conn.execute(
                "SELECT * FROM users WHERE uuid = ?", (uid,)).fetchone()
            return self._to_user(row) if row else None
        finally:
            conn.close()

    def list_users(self):
        conn = self._connect()
        try:
            self._ensure_table(conn)
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at").fetchall()
            return [self._to_user(r) for r in rows]
        finally:
            conn.close()

    def count(self):
        conn = self._connect()
        try:
            self._ensure_table(conn)
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"])
        finally:
            conn.close()

    @staticmethod
    def _safe_get(row, key, default=""):
        try:
            return row[key]
        except (KeyError, IndexError):
            return default

    @staticmethod
    def _to_user(row):
        _g = UserRepository._safe_get
        perms_raw = _g(row, "permissions", "[]")
        try:
            perms = json.loads(perms_raw) if perms_raw else []
        except (json.JSONDecodeError, TypeError):
            perms = []
        return User(
            uuid=row["uuid"], username=row["username"],
            full_name=_g(row, "full_name"),
            phone=row["phone"], email=row["email"],
            password_hash=row["password_hash"], role=row["role"],
            permissions=perms,
            avatar_path=_g(row, "avatar_path"),
            created_at=row["created_at"], updated_at=row["updated_at"],
            disabled=bool(int(_g(row, "disabled", 0) or 0)))
