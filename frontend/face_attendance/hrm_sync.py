"""Send AI face angle images to the SV HRM backend.

The backend endpoint is ``/api/face-recognition/employees/{code}/face-angles/{angle}``
(Spring, ``FaceRecognitionFaceAngleApiController``). It authenticates with an
``X-API-KEY`` header; the key is never put in a URL or an error message, and is
never sent on to another host after a redirect.

No Qt here; the dashboard calls these functions from worker threads.
"""
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .ai_providers import _is_private_host, _multipart, _open

API_PREFIX = "/api/face-recognition"
TIMEOUT_SEC = 60
MAX_IMAGE_BYTES = 8 * 1024 * 1024
SOURCE_AI = "AI_GENERATED"
DEFAULT_HRM_PORT = 8082   # svhrm's server.port in application.properties


def local_ip():
    """This computer's address on the local network (e.g. 192.168.1.20), or 127.0.0.1.

    Connecting a UDP socket sends nothing; it only asks the OS which interface
    would be used, so this works offline and never touches the network.
    """
    for probe in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 1))
                address = sock.getsockname()[0]
        except OSError:
            continue
        if address and not address.startswith(("0.", "127.")):
            return address
    return "127.0.0.1"


def default_base_url():
    """Where HRM runs when no address is saved: svhrm on this Mac, by its network IP."""
    return f"http://{local_ip()}:{DEFAULT_HRM_PORT}"


class HRMError(Exception):
    """A failure that is safe to show the user. ``status`` is the HTTP code, or 0."""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class HRMConfig:
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    auto_upload: bool = True

    @property
    def configured(self):
        return bool(self.base_url.strip() and self.api_key.strip())


def config_from_settings(settings):
    return HRMConfig(base_url=str(getattr(settings, "hrm_api_url", "") or "").strip() or default_base_url(),
                     api_key=str(getattr(settings, "hrm_api_key", "") or "").strip(),
                     auto_upload=bool(getattr(settings, "hrm_auto_upload", True)))


def configuration_problem(config):
    """Why images can't be sent to HRM, in plain words; "" when they can."""
    if not config.base_url.strip():
        return "Add the HRM server address in Settings > AI Configuration to send angles to HRM."
    if not config.api_key.strip():
        return "Add the HRM API key in Settings > AI Configuration to send angles to HRM."
    try:
        _base_url(config)
    except HRMError as exc:
        return str(exc)
    return ""


def plain_http_warning(base_url):
    parts = urllib.parse.urlsplit((base_url or "").strip())
    if parts.scheme != "http" or not parts.hostname or _is_private_host(parts.hostname):
        return ""
    return ("This address uses http://, so the HRM API key and employee face images travel "
            "unencrypted. Use https:// for a server on the internet.")


def _base_url(config):
    url = config.base_url.strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HRMError("The HRM address must start with https:// (for example https://hrm.example.com).")
    if parts.query or parts.fragment:
        raise HRMError("The HRM address must not contain ? or #.")
    return url


def _redact(text, config):
    text = str(text)
    return text.replace(config.api_key, "***") if config.api_key else text


def _message(raw):
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return ""
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or "")[:300]
    return ""


def _status_message(status, detail):
    lead = {401: "HRM rejected the API key.",
            403: "This API key isn't allowed to do that.",
            404: "HRM has no such employee code, or the address is wrong.",
            413: "The image is too large for the HRM server.",
            503: "The HRM face recognition API is switched off (no API key set on the server)."}.get(
        status, f"HRM returned an error (HTTP {status}).")
    if 300 <= status < 400:
        lead = "HRM redirected the request. Check the address (http vs https)."
    return f"{lead} {detail}".strip() if detail and detail not in lead else lead


def _request(config, method, path, body=None, content_type=None, opener=None, raw_bytes=False,
             limit=4 * 1024 * 1024, query=None):
    url = _base_url(config) + API_PREFIX + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
    headers = {"User-Agent": "SV-Face-ID", "Accept": "*/*" if raw_bytes else "application/json",
               "X-API-KEY": config.api_key}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with (opener or _open)(request, TIMEOUT_SEC) as response:
            raw = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise HRMError(_redact(_status_message(exc.code, _message(exc.read(65536))), config), exc.code) from None
    except urllib.error.URLError as exc:
        host = urllib.parse.urlsplit(url).hostname
        raise HRMError(_redact(f"Could not reach HRM at {host}: {exc.reason}", config)) from None
    except TimeoutError:
        raise HRMError("HRM took too long to answer. Try again.") from None
    except OSError as exc:
        raise HRMError(_redact(f"The HRM request failed: {exc}", config)) from None
    if len(raw) > limit:
        raise HRMError("HRM's answer was larger than expected.")
    if raw_bytes:
        return raw
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        raise HRMError("HRM's answer wasn't JSON. Check the address.") from None


def _employee_path(employee_code):
    code = str(employee_code or "").strip()
    if not code:
        raise HRMError("This person has no employee ID, so HRM can't tell who they are.")
    return "/employees/" + urllib.parse.quote(code, safe="")


def check_connection(config, opener=None):
    """Ask HRM for its angle list; returns the angle keys. Proves the address and key work."""
    data = _request(config, "GET", "/face-angles/angles", opener=opener)
    return [row.get("angle") for row in (data.get("data") or []) if isinstance(row, dict)]


def upload_angle(config, employee_code, angle_key, image, source=SOURCE_AI, opener=None):
    """Create or replace one angle image in HRM. *image* is a path or JPEG bytes.

    Returns the stored image's details from HRM (angle, version, sha256, ...).
    """
    content = Path(image).read_bytes() if isinstance(image, (str, Path)) else bytes(image)
    if not content:
        raise HRMError("The image is empty.")
    if len(content) > MAX_IMAGE_BYTES:
        raise HRMError("The image is larger than 8 MB.")
    body, content_type = _multipart({"source": source}, f"{angle_key}.jpg", content, "image/jpeg")
    path = f"{_employee_path(employee_code)}/face-angles/{urllib.parse.quote(angle_key, safe='')}"
    data = _request(config, "PUT", path, body, content_type, opener=opener)
    return data.get("data") or {}


def list_angles(config, employee_code, opener=None):
    """The angles HRM already has for this employee: {"angles": [...], "missing": [...]}."""
    return _request(config, "GET", f"{_employee_path(employee_code)}/face-angles", opener=opener).get("data") or {}


def angle_summary(config, opener=None):
    """Every active employee's stored angles, without images: [{employeeCode, angle, sha256, version}]."""
    rows = _request(config, "GET", "/face-angles/summary", opener=opener).get("data") or []
    return [row for row in rows if isinstance(row, dict)]


def report_angle_checks(config, employee_code, checks, opener=None):
    """Tell HRM how the face check went: checks is [{"angle", "sha256", "status"}, ...]."""
    body = json.dumps({"checks": checks}).encode("utf-8")
    _request(config, "PUT", f"{_employee_path(employee_code)}/face-angles/verification", body=body,
             content_type="application/json", opener=opener)


def angle_image(config, employee_code, angle_key, opener=None):
    """One stored angle image as JPEG bytes."""
    path = f"{_employee_path(employee_code)}/face-angles/{urllib.parse.quote(angle_key, safe='')}/image"
    return bytes(_request(config, "GET", path, opener=opener, raw_bytes=True, limit=MAX_IMAGE_BYTES))


# --- employee directory ------------------------------------------------------

DIRECTORY_PAGE_SIZE = 500     # The server's maximum page size.
DIRECTORY_MAX_PAGES = 200     # 100,000 employees; stops a misbehaving server looping forever.
MAX_PHOTO_BYTES = 10 * 1024 * 1024


def list_employees(config, page=0, size=DIRECTORY_PAGE_SIZE, status="ACTIVE", search="", opener=None):
    """One page of HRM employees: {"employees": [...], "page", "totalPages", "totalElements"}."""
    query = {"page": int(page), "size": int(size), "status": status, "search": search,
             "sortBy": "employee_code", "order": "asc"}
    return _request(config, "GET", "/employees", opener=opener, query=query).get("data") or {}


def fetch_all_employees(config, status="ACTIVE", opener=None, progress=None):
    """Every HRM employee with *status*, page by page. *progress(done, total)* is called after each page."""
    rows, page, seen = [], 0, set()
    while page < DIRECTORY_MAX_PAGES:
        data = list_employees(config, page, DIRECTORY_PAGE_SIZE, status, opener=opener)
        batch = [row for row in (data.get("employees") or []) if isinstance(row, dict)]
        for row in batch:
            key = str(row.get("id") or row.get("employeeCode") or "")
            if key and key in seen:
                continue
            seen.add(key)
            rows.append(row)
        total_pages = int(data.get("totalPages") or 0)
        if progress is not None:
            progress(len(rows), int(data.get("totalElements") or len(rows)))
        page += 1
        if not batch or page >= total_pages:
            break
    return rows


def upload_profile_photo(config, employee_code, image, opener=None):
    """Replace the employee's HRM profile photo with these JPEG bytes."""
    content = bytes(image)
    if not content:
        raise HRMError("The image is empty.")
    if len(content) > MAX_PHOTO_BYTES:
        raise HRMError("The image is larger than 10 MB.")
    body, content_type = _multipart({}, "photo.jpg", content, "image/jpeg")
    _request(config, "PUT", f"{_employee_path(employee_code)}/photo", body, content_type, opener=opener)


def employee_photo(config, employee_code, opener=None):
    """The employee's HRM profile photo as bytes, or None when HRM has none."""
    try:
        data = _request(config, "GET", f"{_employee_path(employee_code)}/photo", opener=opener,
                        raw_bytes=True, limit=MAX_PHOTO_BYTES)
    except HRMError as exc:
        if exc.status == 404:
            return None
        raise
    return bytes(data) or None
