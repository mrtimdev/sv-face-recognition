"""Dashboard sign-in against SV HRM (``/api/auth/login``); HRM owns every account.

The password goes straight to HRM and is never stored. HRM answers with a signed
JWT, which is what a remembered session keeps. A token read back from disk is only
trusted after HRM has accepted it, so editing the session file can't raise anyone's
access. The HRM address itself is deliberately not editable before sign-in: anyone
who could change it could point the app at a server that says they are an admin.

No Qt here; the dashboard calls these functions from worker threads.
"""
import base64
import hashlib
import json
import platform
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import replace

from .ai_providers import _open
from .hrm_sync import HRMError, _base_url, plain_http_warning
from .users import ALL_PERMISSIONS, AccountDisabledError, User

TIMEOUT_SEC = 8
MAX_RESPONSE_BYTES = 1024 * 1024
ADMIN_ROLE = "ROLE_ADMIN"
# Local accounts aren't managed from here any more, so nobody gets manage_users.
_ADMIN_PERMISSIONS = frozenset(ALL_PERMISSIONS) - {"manage_users"}
# Exact HRM authority -> what it allows. An account with none of these can't sign in.
_GRANTS = {
    "ROLE_HR": {"view_dashboard", "view_reports", "manage_employees", "export_data"},
    "employees": {"view_dashboard", "manage_employees"},
    "ROLE_MANAGER": {"view_dashboard", "view_reports"},
    "attendance_reports": {"view_dashboard", "view_reports"},
}


class NoAccessError(Exception):
    """The HRM password was right, but this account has no role that may use Face ID."""


def access_for_roles(roles):
    """(app role, permissions) for these HRM authorities; ("", []) when none grant access."""
    authorities = {str(role) for role in roles or ()}
    if ADMIN_ROLE in authorities:
        return "admin", [name for name in ALL_PERMISSIONS if name in _ADMIN_PERMISSIONS]
    granted = set().union(*(_GRANTS.get(authority, set()) for authority in authorities)) if authorities else set()
    return ("user", [name for name in ALL_PERMISSIONS if name in granted]) if granted else ("", [])


def _device_info(app_version):
    # HRM only accepts a token later if the login that made it registered a device.
    return {"deviceId": "sv-face-id-" + hashlib.sha256(str(uuid.getnode()).encode()).hexdigest()[:24],
            "deviceModel": "SV Face ID desktop", "platform": platform.system(),
            "osVersion": platform.release(), "appVersion": str(app_version or "")}


def _call(config, method, path, body=None, token="", opener=None, query=None):
    """(status, parsed JSON or {}). Raises ``HRMError`` when HRM can't be reached."""
    url = _base_url(config) + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"User-Agent": "SV-Face-ID", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with (opener or _open)(request, TIMEOUT_SEC) as response:
            status, raw = response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(65536)
    except urllib.error.URLError as exc:
        host = urllib.parse.urlsplit(url).hostname
        raise HRMError(f"Could not reach SV HRM at {host}: {exc.reason}") from None
    except TimeoutError:
        raise HRMError("SV HRM took too long to answer. Try again.") from None
    except OSError as exc:
        raise HRMError(f"The SV HRM request failed: {exc}") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HRMError("SV HRM's answer was larger than expected.")
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _build_user(user_id, username, full_name, roles, token):
    role, permissions = access_for_roles(roles)
    return User(uuid=f"hrm:{user_id or username}", username=username, full_name=full_name or username,
                phone="", email="", password_hash="", role=role, permissions=permissions, hrm_token=token)


def sign_in(config, username, password, app_version="", opener=None, on_stage=None):
    """The signed-in ``User``, or None for a wrong username or password.

    Raises ``AccountDisabledError``, ``NoAccessError``, or ``HRMError`` when HRM
    can't answer. Both failed-login cases look the same to the person signing in,
    so this screen doesn't reveal which usernames exist.
    """
    _base_url(config)
    warning = plain_http_warning(config.base_url)
    if warning:
        raise HRMError("Passwords can't be sent to this SV HRM address over http://. Use https://.")
    body = {"username": username, "password": password, "deviceInfo": _device_info(app_version)}
    status, data = _call(config, "POST", "/api/auth/login", body, opener=opener)
    if on_stage is not None:
        on_stage("verify")
    if status == 401:
        message = str(data.get("message") or "")
        if data.get("errorType") in ("username", "password"):
            return None
        if "disabled" in message.lower():
            raise AccountDisabledError(User("", username, "", "", "", "", "user"))
        raise HRMError(message[:300] or "SV HRM refused this sign-in.")
    token = data.get("accessToken")
    if status != 200 or not isinstance(token, str) or not token:
        raise HRMError(f"SV HRM returned an unexpected answer (HTTP {status}). Check its address in settings.json.")
    user = _build_user(data.get("userId"), str(data.get("username") or username),
                       str(data.get("employeeName") or ""), data.get("roles"), token)
    if not user.permissions:
        raise NoAccessError(username)
    return user


def token_claims(token):
    """The token's payload, unverified: only trust it once HRM has accepted the token."""
    try:
        payload = str(token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def token_expired(token, now=None):
    """True for an unreadable token or one past its ``exp``.

    SV HRM's access tokens currently carry no ``exp``: they last until HRM deactivates the
    device's session. So a token without one isn't expired, and HRM's own answer decides.
    """
    claims = token_claims(token)
    if not claims:
        return True
    if "exp" not in claims:
        return False
    expires = claims["exp"]
    return not isinstance(expires, (int, float)) or expires <= (now or time.time())


def check_account(config, user, opener=None):
    """The account as HRM sees it now: *user*, a disabled copy, or None once its session ended.

    Raises ``HRMError`` when that can't be determined (HRM offline, or the token
    simply ran out, which is no reason to stop a running dashboard).
    """
    if token_expired(user.hrm_token):
        raise HRMError("The SV HRM session has expired.")
    status, data = _call(config, "GET", "/api/users/check-username", token=user.hrm_token, opener=opener,
                         query={"username": user.username})
    if status == 401:
        return None
    if status != 200 or "exists" not in data:
        raise HRMError(f"SV HRM could not check this account (HTTP {status}).")
    return user if data["exists"] else replace(user, disabled=True)


def restore(config, session, opener=None):
    """The remembered ``User`` once HRM accepts its token, else None. ``HRMError`` if HRM is offline."""
    token = getattr(session, "hrm_token", "")
    if not token or token_expired(token):
        return None
    claims = token_claims(token)
    username = str(claims.get("sub") or "")
    if not username or username != session.username:
        return None
    user = _build_user(session.user_uuid.removeprefix("hrm:"), username, session.full_name, claims.get("roles"), token)
    if not user.permissions:
        return None
    fresh = check_account(config, user, opener=opener)
    return fresh if fresh is not None and not fresh.disabled else None
