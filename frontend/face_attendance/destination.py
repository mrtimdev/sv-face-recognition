"""Conservative outbox routing: a different origin/path is a different destination.

Do not infer tenant equivalence from API keys or a public ping response. An
operator can explicitly reassign reviewed events using the maintenance command.
"""
from urllib.parse import urlsplit, urlunsplit


def destination_id(url):
    if not url:
        return ""
    parts = urlsplit(str(url).strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Attendance destination must be an HTTP(S) URL without credentials")
    if parts.query or parts.fragment:
        raise ValueError("Attendance destination cannot contain a query or fragment")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port and port != {"http": 80, "https": 443}[parts.scheme]:
        host += f":{port}"
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))
