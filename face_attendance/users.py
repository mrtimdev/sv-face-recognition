"""User accounts with role-based access (USER / ADMIN).

Passwords are hashed with PBKDF2-SHA256 + random salt.  The ``UserRepository``
handles CRUD against the ``users`` table through the ``database`` abstraction.
"""
import hashlib
import json
import os
import secrets
import tempfile
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
# First sign-in for a released app whose user database has no accounts yet.
DEFAULT_ADMIN_USERNAME = "timdev"
DEFAULT_ADMIN_PASSWORD = "00000000"


class AccountDisabledError(Exception):
    """Raised by ``authenticate`` when the password is right but the account is disabled."""

    def __init__(self, user):
        super().__init__(f"The account '{user.username}' is disabled.")
        self.user = user


PASSWORD_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not isinstance(password, str) or not isinstance(stored, str):
        return False
    try:
        if stored.startswith("pbkdf2_sha256$"):
            _, iterations, salt_hex, key_hex = stored.split("$")
            iterations = int(iterations)
            if not 100_000 <= iterations <= 2_000_000:
                return False
        else:
            salt_hex, key_hex = stored.split(":", 1)
            iterations = 100_000  # Backward compatible; upgraded on sign-in.
        salt = bytes.fromhex(salt_hex)
        if len(salt) != 16 or len(bytes.fromhex(key_hex)) != 32:
            return False
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return secrets.compare_digest(key.hex(), key_hex)
    except (ValueError, TypeError):
        # A broken hashing library still raises, so it is reported as a failure
        # to check the password instead of looking like a mistyped one.
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
    hrm_token: str = field(default="", repr=False)  # Set for an account signed in through SV HRM.

    @property
    def external(self) -> bool:
        """True when SV HRM owns this account, not the local ``users`` table."""
        return bool(self.hrm_token)

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
    hrm_token: str = field(default="", repr=False)


def save_session(user: User) -> Session:
    token = secrets.token_hex(32)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session = Session(
        user_uuid=user.uuid, username=user.username,
        full_name=user.full_name, role=user.role,
        token=token, created_at=now, hrm_token=user.hrm_token,
    )
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "user_uuid": session.user_uuid,
        "username": session.username,
        "full_name": session.full_name,
        "role": session.role,
        "token": session.token,
        "created_at": session.created_at,
        "hrm_token": session.hrm_token,
    })
    # The temporary file is private from creation, and replacement is atomic.
    # A crash cannot leave a truncated session or a briefly public token.
    with tempfile.NamedTemporaryFile(mode="w", dir=SESSION_PATH.parent,
                                     encoding="utf-8", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, SESSION_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        os.chmod(SESSION_PATH, 0o600)
    except OSError:
        pass
    return session


def load_session() -> Session | None:
    if not SESSION_PATH.exists():
        return None
    try:
        with SESSION_PATH.open(encoding="utf-8") as handle:
            payload = handle.read(65_537)
        if len(payload) > 65_536:
            raise ValueError("Session file too large")
        data = json.loads(payload)
        if not isinstance(data, dict) or not all(
                isinstance(data.get(key), str) and data[key]
                for key in ('user_uuid', 'username', 'token', 'created_at')):
            raise ValueError("Invalid session")
        if len(data['token']) != 64 or len(bytes.fromhex(data['token'])) != 32:
            raise ValueError("Invalid session token")
        created = datetime.fromisoformat(data['created_at'])
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if not -300 <= age <= 30 * 86400:
            raise ValueError("Remembered session expired")
        return Session(
            user_uuid=data["user_uuid"], username=data["username"],
            full_name=data.get("full_name", ""), role=data.get("role", "user"),
            token=data["token"], created_at=data["created_at"],
            hrm_token=str(data.get("hrm_token") or ""),
        )
    except (ValueError, TypeError, KeyError, OSError):
        try:
            clear_session()
        except OSError:
            pass
        return None


def clear_session():
    SESSION_PATH.unlink(missing_ok=True)


class UserRepository:
    def __init__(self, settings):
        self._settings = settings

    def _connect(self):
        return connect(self._settings)

    def _ensure_table(self, conn):
        if conn.table_exists("users"):
            cols = {c.lower() for c in conn.get_columns("users")}
            text = "VARCHAR(255)" if conn.backend == "mysql" else "TEXT"
            long_text = "VARCHAR(1024)" if conn.backend == "mysql" else "TEXT"  # MySQL: no default on TEXT
            migrations = [
                ("full_name", f"{text} NOT NULL DEFAULT ''"),
                ("avatar_path", f"{long_text} NOT NULL DEFAULT ''"),
                ("permissions", f"{long_text} NOT NULL DEFAULT '[]'"),
                ("disabled", "INTEGER NOT NULL DEFAULT 0"),
            ]
            for col, typedef in migrations:
                if col not in cols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
                    conn.commit()
            return
        text = "VARCHAR(255)" if conn.backend == "mysql" else "TEXT"
        long_text = "VARCHAR(1024)" if conn.backend == "mysql" else "TEXT"  # MySQL: no default on TEXT
        pk = f"{text} PRIMARY KEY" if conn.backend == "mysql" else "TEXT PRIMARY KEY"
        conn.execute(f"""CREATE TABLE IF NOT EXISTS users (
            uuid {pk},
            username {text} NOT NULL,
            full_name {text} NOT NULL DEFAULT '',
            phone {text} NOT NULL DEFAULT '',
            email {text} NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            role {text} NOT NULL DEFAULT 'user',
            permissions {long_text} NOT NULL DEFAULT '[]',
            avatar_path {long_text} NOT NULL DEFAULT '',
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
                if not row["password_hash"].startswith(f"pbkdf2_sha256${PASSWORD_ITERATIONS}$"):
                    upgraded = hash_password(password)
                    conn.execute("UPDATE users SET password_hash=? WHERE uuid=? AND password_hash=?",
                                 (upgraded, user.uuid, row["password_hash"]))
                    conn.commit()
                    user.password_hash = upgraded
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
