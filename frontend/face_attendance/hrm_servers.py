"""Which SV HRM address the app talks to: the internet one, the office one, or whichever answers.

The choice is three settings (mode and two addresses). Reading the current address
never touches the network, because it is read on the UI thread too; only ``resolve``
probes, and the dashboard calls that from worker threads. A probe goes to HRM's public
ping and carries no API key, token or password, so a wrong address learns nothing.

No Qt here.
"""
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .ai_providers import _is_private_host, _open
from .hrm_sync import HRMConfig, HRMError, _base_url, default_base_url

AUTO = "auto"
ONLINE = "online"
OFFICE = "office"
MODES = (AUTO, ONLINE, OFFICE)
LABELS = {AUTO: "Automatic", ONLINE: "Online", OFFICE: "Office"}
DESCRIPTIONS = {
    AUTO: "The office network when you're there, the internet anywhere else.",
    ONLINE: "Always over the internet.",
    OFFICE: "Only on the office network.",
}
NETWORK_NAMES = {ONLINE: "Internet", OFFICE: "Office network"}

DEFAULT_ONLINE_URL = "https://uathrms.svtrucking.biz"
PING_PATH = "/api/public/ping"
PING_APP = "svhrm"
PROBE_TIMEOUT_SEC = 5
AUTO_OFFICE_TIMEOUT_SEC = 1.5   # Away from the office this address never answers; don't wait long.
MAX_PING_BYTES = 4096

_chosen = {}   # (online url, office url) -> the url Automatic last found working


@dataclass(frozen=True)
class Profile:
    mode: str = AUTO
    online: str = ""   # Empty means the default address.
    office: str = ""

    @property
    def online_url(self):
        return self.online or DEFAULT_ONLINE_URL

    @property
    def office_url(self):
        return self.office or default_base_url()

    def candidates(self):
        """(network, url) in the order they are tried."""
        online, office = (ONLINE, self.online_url), (OFFICE, self.office_url)
        return {ONLINE: (online,), OFFICE: (office,)}.get(self.mode, (office, online))


@dataclass(frozen=True)
class Connection:
    network: str
    url: str
    ok: bool
    ms: int = 0
    detail: str = ""


def normalize_url(text):
    """The server's root; mobile-style addresses ending in /api are accepted too."""
    url = str(text or "").strip().rstrip("/")
    for suffix in ("/api/face-recognition", "/api"):
        if url.lower().endswith(suffix):
            return url[:-len(suffix)].rstrip("/")
    return url


def address_problem(text):
    """Why this address can't be used, in plain words; "" when it can (empty uses the default)."""
    url = normalize_url(text)
    if not url:
        return ""
    try:
        _base_url(HRMConfig(base_url=url))
    except HRMError as exc:
        return str(exc)
    return ""


def is_office_address(url):
    return _is_private_host(urllib.parse.urlsplit(url).hostname or "")


def profile(settings):
    """The saved choice. An address saved before modes existed stays the only one used."""
    mode = str(getattr(settings, "hrm_server_mode", "") or "")
    mode = mode if mode in MODES else ""
    online = normalize_url(getattr(settings, "hrm_online_url", ""))
    office = normalize_url(getattr(settings, "hrm_office_url", ""))
    legacy = normalize_url(getattr(settings, "hrm_api_url", ""))
    if legacy and not (mode or online or office):
        if is_office_address(legacy):
            return Profile(OFFICE, office=legacy)
        return Profile(ONLINE, online=legacy)
    return Profile(mode or AUTO, online, office)


def to_settings(choice):
    """Settings values for this choice; the old single address is replaced by it."""
    return {"hrm_server_mode": choice.mode, "hrm_online_url": choice.online,
            "hrm_office_url": choice.office, "hrm_api_url": ""}


def single_address_settings(address):
    """Settings values for one fixed address, as the first-run wizard asks for it."""
    return {"hrm_server_mode": "", "hrm_online_url": "", "hrm_office_url": "",
            "hrm_api_url": normalize_url(address)}


def current_url(settings):
    """The address to use now. Never touches the network."""
    choice = profile(settings)
    if choice.mode != AUTO:
        return choice.candidates()[0][1]
    return _chosen.get((choice.online_url, choice.office_url)) or choice.office_url


def network_of(choice, url):
    """Which of this choice's networks an address belongs to; "" when it is neither."""
    return next((network for network, candidate in choice.candidates() if candidate == url), "")


def probe(network, url, timeout=PROBE_TIMEOUT_SEC, opener=None):
    """Ask one address whether SV HRM is there; never raises and never sends a credential."""
    problem = address_problem(url)
    if problem:
        return Connection(network, url, False, detail=problem)
    request = urllib.request.Request(url + PING_PATH, method="GET",
                                     headers={"User-Agent": "SV-Face-ID", "Accept": "application/json"})
    started = time.monotonic()
    try:
        with (opener or _open)(request, timeout) as response:
            raw = response.read(MAX_PING_BYTES)
    except urllib.error.HTTPError as exc:
        return Connection(network, url, False, detail=f"{host_of(url)} isn't SV HRM (HTTP {exc.code}).")
    except urllib.error.URLError as exc:
        return Connection(network, url, False, detail=f"Could not reach {host_of(url)}: {exc.reason}")
    except TimeoutError:
        return Connection(network, url, False, detail=f"{host_of(url)} took too long to answer.")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return Connection(network, url, False, detail=f"Could not reach {host_of(url)}: {exc}")
    ms = int((time.monotonic() - started) * 1000)
    try:
        app = json.loads(raw.decode("utf-8")).get("app")
    except (ValueError, AttributeError):
        app = None
    if app != PING_APP:
        return Connection(network, url, False, ms, f"{host_of(url)} answered, but it isn't SV HRM.")
    return Connection(network, url, True, ms)


def resolve(choice, opener=None):
    """Find the address to use for this choice. Automatic remembers what it found."""
    automatic = choice.mode == AUTO
    results = []
    for network, url in choice.candidates():
        timeout = AUTO_OFFICE_TIMEOUT_SEC if automatic and network == OFFICE else PROBE_TIMEOUT_SEC
        connection = probe(network, url, timeout, opener)
        if connection.ok:
            if automatic:
                _chosen[(choice.online_url, choice.office_url)] = url
            return connection
        results.append(connection)
    if not automatic:
        return results[0]
    return Connection(results[0].network, results[0].url, False,
                      detail="Neither the office nor the internet address answered.")


def host_of(url):
    return urllib.parse.urlsplit(url).netloc or url


def status_lines(connection):
    """(headline, detail) describing a check's result to a person."""
    if not connection.ok:
        return "Can't connect", connection.detail
    return (f"Connected · {NETWORK_NAMES.get(connection.network, 'Server')}",
            f"{host_of(connection.url)} · {connection.ms} ms")
