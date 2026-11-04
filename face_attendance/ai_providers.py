"""AI providers for the dashboard: list models, send a test message, generate face angles.

Plain HTTP so one code path covers GPT, Gemini, Claude and any OpenAI-compatible
endpoint, without a vendor SDK to bundle per provider. No Qt here; the dashboard
calls these functions from worker threads. API keys go in request headers only
and are removed from every error message.
"""
import base64
import http.client
import ipaddress
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .enrollment_photos import safe_label
from .storage import write_jpeg

ANTHROPIC_VERSION = "2026-10-21"
MODEL_TIMEOUT_SEC = 30
MESSAGE_TIMEOUT_SEC = 120
IMAGE_TIMEOUT_SEC = 300
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_SIDE = 1536
DEFAULT_TEST_MESSAGE = "Say hello in one short sentence."
AI_ANGLES_DIR = "ai_angles"


class AIError(Exception):
    """A failure that is safe to show the user. ``status`` is the HTTP code, or 0."""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str
    can_generate_images: bool
    key_required: bool = True


PROVIDERS = {
    "openai": Provider("openai", "GPT (OpenAI)", "https://api.openai.com/v1", True),
    "gemini": Provider("gemini", "Gemini (Google)", "https://generativelanguage.googleapis.com/v1beta", True),
    # Claude reads images and answers in text; the API has no image generation.
    "claude": Provider("claude", "Claude (Anthropic)", "https://api.anthropic.com/v1", False),
    "custom": Provider("custom", "Custom (OpenAI-compatible)", "", True, key_required=False),
}
PREFERRED_CHAT_MODELS = {"claude": "claude-opus-5-5"}


@dataclass(frozen=True)
class AIConfig:
    provider: str = "openai"
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    chat_model: str = ""
    image_model: str = ""

    @property
    def info(self):
        try:
            return PROVIDERS[self.provider]
        except KeyError:
            raise AIError(f"Unknown AI provider: {self.provider}") from None


# --- stored configuration ---------------------------------------------------
# Each provider keeps its own key and models, so switching provider can never
# send one vendor's key to another.
PROFILE_FIELDS = ("api_key", "base_url", "chat_model", "image_model")


def load_profiles(settings):
    try:
        stored = json.loads(getattr(settings, "ai_profiles", "") or "{}")
    except ValueError:
        stored = {}
    stored = stored if isinstance(stored, dict) else {}
    profiles = {}
    for key in PROVIDERS:
        entry = stored.get(key)
        entry = entry if isinstance(entry, dict) else {}
        profiles[key] = {name: str(entry.get(name) or "") for name in PROFILE_FIELDS}
    return profiles


def dump_profiles(profiles):
    return json.dumps({key: {name: str(entry.get(name) or "") for name in PROFILE_FIELDS}
                       for key, entry in profiles.items() if key in PROVIDERS})


def config_from_settings(settings):
    provider = getattr(settings, "ai_provider", "openai")
    provider = provider if provider in PROVIDERS else "openai"
    return AIConfig(provider=provider, **load_profiles(settings)[provider])


def configuration_problem(config):
    """Why *config* can't generate images, in plain words; "" when it can."""
    info = config.info
    if not info.can_generate_images:
        return (f"{info.label} can't generate images. Choose GPT, Gemini or Custom "
                "in Settings > AI Configuration.")
    if info.key_required and not config.api_key:
        return f"Add your {info.label} API key in Settings > AI Configuration."
    if not info.base_url and not config.base_url:
        return "Add the Custom provider's base URL in Settings > AI Configuration."
    if not config.image_model:
        return "Choose an image model in Settings > AI Configuration."
    return ""


def provider_host(config):
    """The host a request goes to, for telling the user where a photo is sent."""
    url = config.info.base_url or config.base_url
    return urllib.parse.urlsplit(url.strip()).hostname or "the provider"


# --- HTTP -------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib repeats custom headers on a redirect, which would hand the key to another host."""

    def redirect_request(self, *_args, **_kwargs):
        return None


def _ssl_context():
    # Packaged builds may have no system CA path; certifi ships with them.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        return ssl.create_default_context()


def _open(request, timeout, follow_redirects=False):
    handlers = [urllib.request.HTTPSHandler(context=_ssl_context())]
    if not follow_redirects:
        handlers.append(_NoRedirect())
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)


def _base_url(config):
    url = (config.info.base_url or config.base_url or "").strip().rstrip("/")
    if not url:
        raise AIError("Enter the provider's base URL, for example https://example.com/v1")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AIError("The base URL must start with https://")
    return url


def plain_http_warning(base_url):
    """A warning when a base URL would expose the key and photos on the network; "" otherwise."""
    parts = urllib.parse.urlsplit((base_url or "").strip())
    if parts.scheme != "http" or not parts.hostname or _is_private_host(parts.hostname):
        return ""
    return ("This address uses http://, so your API key and employee photos travel unencrypted "
            "and could be read on the network. Use https:// if the provider offers it.")


def _is_private_host(host):
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def _auth_headers(config):
    if not config.api_key:
        if config.info.key_required:
            raise AIError(f"Enter your {config.info.label} API key first.")
        return {}
    if config.provider == "gemini":
        return {"x-goog-api-key": config.api_key}
    if config.provider == "claude":
        return {"x-api-key": config.api_key, "anthropic-version": ANTHROPIC_VERSION}
    return {"Authorization": f"Bearer {config.api_key}"}


def _redact(text, config):
    text = str(text)
    return text.replace(config.api_key, "***") if config.api_key else text


def _error_detail(raw):
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return ""
    data = data[0] if isinstance(data, list) and data else data
    if not isinstance(data, dict):
        return ""
    error = data.get("error", data)
    if isinstance(error, dict):
        error = error.get("message") or error.get("detail") or ""
    return str(error or data.get("message") or "")[:400]


def _status_message(status, detail):
    if 300 <= status < 400:
        return "The provider redirected the request. Check the base URL."
    lead = {401: "The provider rejected the API key.",
            402: "The provider account has no credit.",
            403: "This API key isn't allowed to do that.",
            404: "Not found. Check the base URL and the model name.",
            408: "The provider timed out. Try again.",
            413: "The photo is too large for this provider.",
            429: "Rate limit or quota reached. Wait a moment or check your plan."}.get(
        status, f"The provider returned an error (HTTP {status}).")
    return f"{lead} {detail}".strip() if detail else lead


def _request(config, method, url, headers=None, body=None, timeout=MODEL_TIMEOUT_SEC):
    all_headers = {"User-Agent": "SV-Face-ID", "Accept": "application/json",
                   **_auth_headers(config), **(headers or {})}
    request = urllib.request.Request(url, data=body, headers=all_headers, method=method)
    try:
        with _open(request, timeout) as response:
            return response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc.read(65536))
        raise AIError(_redact(_status_message(exc.code, detail), config), exc.code) from None
    except urllib.error.URLError as exc:
        host = urllib.parse.urlsplit(url).hostname
        raise AIError(_redact(f"Could not reach {host}: {exc.reason}", config)) from None
    except TimeoutError:
        raise AIError("The provider took too long to answer. Try again.") from None
    except (http.client.HTTPException, OSError, ValueError) as exc:
        raise AIError(_redact(f"The request failed: {exc}", config)) from None


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise AIError("The provider's answer wasn't JSON. Check the base URL; "
                      "it usually ends in /v1.") from None


def _post_json(config, url, payload, timeout):
    return _json(_request(config, "POST", url, {"Content-Type": "application/json"},
                          json.dumps(payload).encode("utf-8"), timeout))


# --- models -----------------------------------------------------------------
class ModelList(list):
    """Model ids, plus what the provider said about them. Many gateways report types."""

    def __init__(self, ids=(), kinds=None):
        super().__init__(ids)
        self.kinds = kinds or {}  # id -> "text" | "image" | "other"


def _reported_kind(entry):
    outputs = entry.get("output_modalities")
    outputs = outputs if isinstance(outputs, list) else []
    kind = str(entry.get("type") or "").lower()
    if kind == "image" or "image" in outputs:
        return "image"
    if kind in ("video", "audio", "embedding") or (outputs and "text" not in outputs):
        return "other"
    return "text" if "text" in outputs else ""


def _described_models(entries):
    ids, kinds = [], {}
    for entry in entries:
        if not isinstance(entry, dict):
            ids.append(str(entry))
            continue
        model = entry.get("id", "")
        ids.append(model)
        kind = _reported_kind(entry)
        if kind:
            kinds[model] = kind
    return ModelList(sorted({model for model in ids if model}), kinds)


def list_models(config):
    """Every model id the provider offers this key, sorted."""
    base = _base_url(config)
    if config.provider == "gemini":
        data = _json(_request(config, "GET", f"{base}/models?pageSize=1000"))
        names = [entry.get("name", "") for entry in data.get("models", [])
                 if "generateContent" in entry.get("supportedGenerationMethods", ())]
        models = [name.removeprefix("models/") for name in names]
    elif config.provider == "claude":
        data = _json(_request(config, "GET", f"{base}/models?limit=1000"))
        models = [entry.get("id", "") for entry in data.get("data", [])]
    else:
        data = _json(_request(config, "GET", f"{base}/models"))
        entries = data if isinstance(data, list) else data.get("data") or data.get("models") or []
        models = _described_models(entries)
    models = models if isinstance(models, ModelList) else ModelList(sorted({model for model in models if model}))
    if not models:
        raise AIError("The provider returned no models for this key.")
    return models


_IMAGE_MODEL = re.compile(r"image|dall-?e|imagen|flux|stable-?diffusion|sdxl|recraft|ideogram", re.I)
# Families known to edit an input photo. Gateways' input_modalities labels proved unreliable for this.
_PHOTO_EDIT_MODEL = re.compile(r"gpt-image|gemini.*image|nano-?banana|kontext|seedream", re.I)
_NOT_CHAT_MODEL = re.compile(r"embed|whisper|tts|moderation|transcribe|realtime|sora|-audio", re.I)


def split_models(models):
    """(chat models, image models). Uses what the provider reports, then the name.

    Models known to edit a photo come first: only they can keep a face.
    Either list falls back to everything.
    """
    kinds = getattr(models, "kinds", {})
    image, chat = [], []
    for model in models:
        kind = kinds.get(model)
        if kind is None:
            kind = ("image" if _IMAGE_MODEL.search(model) else
                    "other" if _NOT_CHAT_MODEL.search(model) else "text")
        if kind == "image":
            image.append(model)
        elif kind == "text":
            chat.append(model)
    image.sort(key=lambda model: not _PHOTO_EDIT_MODEL.search(model))
    return chat or list(models), image or list(models)


# --- test message -----------------------------------------------------------
def send_test_message(config, message=DEFAULT_TEST_MESSAGE):
    """Send *message* to the chat model and return the reply text."""
    base = _base_url(config)
    if not config.chat_model:
        raise AIError("Choose a chat model first. Use Load models to see what's available.")
    message = str(message or DEFAULT_TEST_MESSAGE)
    if config.provider == "gemini":
        data = _post_json(config, f"{base}/models/{_quoted_model(config.chat_model)}:generateContent",
                          {"contents": [{"parts": [{"text": message}]}]}, MESSAGE_TIMEOUT_SEC)
        _raise_if_gemini_blocked(data)
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        reply = "".join(part.get("text", "") for part in parts)
    elif config.provider == "claude":
        # Room for thinking on models that always think; the reply itself is short.
        data = _post_json(config, f"{base}/messages",
                          {"model": config.chat_model, "max_tokens": 1024,
                           "messages": [{"role": "user", "content": message}]}, MESSAGE_TIMEOUT_SEC)
        if data.get("stop_reason") == "refusal":
            raise AIError("Claude declined this message.")
        reply = "".join(block.get("text", "") for block in data.get("content", [])
                        if block.get("type") == "text")
    else:
        data = _post_json(config, f"{base}/chat/completions",
                          {"model": config.chat_model,
                           "messages": [{"role": "user", "content": message}]}, MESSAGE_TIMEOUT_SEC)
        content = _chat_message(data).get("content")
        reply = content if isinstance(content, str) else "".join(
            part.get("text", "") for part in content or [] if isinstance(part, dict))
    reply = reply.strip()
    if not reply:
        raise AIError("The model answered with no text. Try another chat model.")
    return reply


def _quoted_model(model):
    return urllib.parse.quote(model.removeprefix("models/"), safe="-._")


def _chat_message(data):
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise AIError("The provider's answer had no message in it.")
    return choices[0].get("message") or {}


def _raise_if_gemini_blocked(data):
    reason = (data.get("promptFeedback") or {}).get("blockReason")
    if reason:
        raise AIError(f"Gemini blocked this request ({reason}).")


# --- angles -----------------------------------------------------------------
@dataclass(frozen=True)
class Angle:
    key: str
    label: str
    instruction: str


ANGLES = (
    Angle("left_30", "30° left",
          "the head is turned 30 degrees toward the person's own left, a three-quarter view"),
    Angle("right_30", "30° right",
          "the head is turned 30 degrees toward the person's own right, a three-quarter view"),
    Angle("left_90", "90° left",
          "the head is turned 90 degrees toward the person's own left, a full side profile"),
    Angle("right_90", "90° right",
          "the head is turned 90 degrees toward the person's own right, a full side profile"),
    Angle("chin_up", "Chin up",
          "the head is tilted back with the chin raised about 20 degrees, still facing the camera"),
    Angle("chin_down", "Chin down",
          "the head is tilted forward with the chin lowered about 20 degrees, still facing the camera"),
)


def angle_prompt(angle):
    return ("Using the attached photo as the only identity reference, create a photorealistic 1:1 "
            "head-and-shoulders portrait of this exact same person. Keep the face unchanged: the same "
            "eye shape, eyebrows, nose, lips, jawline, ears, skin tone and natural skin texture, with "
            "any marks, facial hair and glasses. Keep the same hairstyle, clothing, neutral expression, "
            "background and soft even light. A single person alone in the frame. "
            f"Only the head angle changes: {angle.instruction}. Sharp focus, 85mm portrait lens, "
            "no retouching, no text, no watermark.")


def encode_source(image):
    """JPEG bytes of the source photo, small enough for every provider."""
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest > MAX_SOURCE_SIDE:
        scale = MAX_SOURCE_SIDE / longest
        image = cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise AIError("The photo could not be prepared.")
    return encoded.tobytes()


def generate_image(config, source_jpeg, prompt):
    """Image bytes in whatever format the provider returns."""
    if not config.info.can_generate_images:
        raise AIError(f"{config.info.label} can't generate images. Choose GPT, Gemini or Custom.")
    if not config.image_model:
        raise AIError("Choose an image model first.")
    if config.provider == "gemini":
        return _gemini_image(config, source_jpeg, prompt)
    try:
        return _openai_image(config, source_jpeg, prompt)
    except AIError as exc:
        # Many OpenAI-compatible gateways serve image models through chat only.
        if config.provider == "custom" and exc.status in (404, 405, 501):
            return _chat_image(config, source_jpeg, prompt)
        raise


def _multipart(fields, filename, content, content_type):
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
                      .encode("utf-8"))
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
                  f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    chunks += [content, f"\r\n--{boundary}--\r\n".encode("utf-8")]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _openai_image(config, source_jpeg, prompt):
    url = f"{_base_url(config)}/images/edits"
    fields = {"model": config.image_model, "prompt": prompt, "size": "1024x1024", "n": "1"}
    if config.image_model.startswith("gpt-image"):
        fields["input_fidelity"] = "high"  # Holds the face closer to the source photo.
    for attempt in range(2):
        body, content_type = _multipart(fields, "photo.jpg", source_jpeg, "image/jpeg")
        try:
            data = _json(_request(config, "POST", url, {"Content-Type": content_type}, body, IMAGE_TIMEOUT_SEC))
            break
        except AIError as exc:
            if attempt == 0 and exc.status == 400 and "input_fidelity" in str(exc):
                fields.pop("input_fidelity", None)  # Not every model accepts it.
                continue
            raise
    entries = data.get("data") if isinstance(data, dict) else None
    if not entries:
        raise AIError("The provider's answer had no image in it.")
    entry = entries[0]
    if entry.get("b64_json"):
        return _b64(entry["b64_json"])
    if entry.get("url"):
        return _image_at(config, entry["url"])
    raise AIError("The provider's answer had no image in it.")


_IMAGE_REFERENCE = re.compile(r"data:image/[\w.+-]+;base64,[A-Za-z0-9+/=\s]+|https?://[^\s)\"']+")


def _chat_image(config, source_jpeg, prompt):
    data_url = "data:image/jpeg;base64," + base64.b64encode(source_jpeg).decode("ascii")
    data = _post_json(config, f"{_base_url(config)}/chat/completions", {
        "model": config.image_model,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
    }, IMAGE_TIMEOUT_SEC)
    message = _chat_message(data)
    content = message.get("content")
    candidates = list(message.get("images") or [])
    text = content if isinstance(content, str) else ""
    if isinstance(content, list):
        candidates += [part for part in content if isinstance(part, dict)]
        text = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    for candidate in candidates:
        reference = candidate.get("image_url") or candidate.get("url") or ""
        reference = reference.get("url", "") if isinstance(reference, dict) else reference
        if reference:
            return _image_at(config, reference)
    match = _IMAGE_REFERENCE.search(text)
    if match:
        return _image_at(config, match.group(0))
    raise AIError("The model replied with text instead of an image. Choose an image model. "
                  + _redact(text.strip()[:200], config))


def _gemini_image(config, source_jpeg, prompt):
    url = f"{_base_url(config)}/models/{_quoted_model(config.image_model)}:generateContent"
    payload = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(source_jpeg).decode("ascii")}},
            {"text": prompt},
        ]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": "1:1"}},
    }
    try:
        data = _post_json(config, url, payload, IMAGE_TIMEOUT_SEC)
    except AIError as exc:
        if exc.status != 400 or "imageconfig" not in str(exc).replace("_", "").lower():
            raise
        del payload["generationConfig"]["imageConfig"]  # Older models; the result is cropped square anyway.
        data = _post_json(config, url, payload, IMAGE_TIMEOUT_SEC)
    _raise_if_gemini_blocked(data)
    candidate = (data.get("candidates") or [{}])[0]
    text = []
    for part in candidate.get("content", {}).get("parts", []):
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return _b64(inline["data"])
        text.append(part.get("text", ""))
    reason = candidate.get("finishReason", "")
    if reason and reason not in ("STOP", "MAX_TOKENS"):
        raise AIError(f"Gemini didn't create the image ({reason}).")
    raise AIError("The model replied with text instead of an image. Choose an image model. "
                  + _redact("".join(text).strip()[:200], config))


def _b64(data):
    try:
        return base64.b64decode(data, validate=False)
    except ValueError:
        raise AIError("The provider's image data was damaged.") from None


def _image_at(config, reference):
    """Bytes from a data URL or a download link; the API key never goes to the link's host."""
    reference = reference.strip()
    if reference.startswith("data:"):
        return _b64(reference.partition(",")[2])
    if not reference.lower().startswith(("https://", "http://")):
        raise AIError("The provider's image link wasn't usable.")
    request = urllib.request.Request(reference, headers={"User-Agent": "SV-Face-ID"})
    try:
        with _open(request, IMAGE_TIMEOUT_SEC, follow_redirects=True) as response:
            return response.read(MAX_RESPONSE_BYTES)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise AIError(_redact(f"The generated image could not be downloaded: {exc}", config)) from None


def square_image(data):
    """Decode a generated image and centre-crop it to exactly 1:1."""
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR) if data else None
    if image is None:
        raise AIError("The provider's answer wasn't a readable image.")
    height, width = image.shape[:2]
    side = min(height, width)
    top, left = (height - side) // 2, (width - side) // 2
    return image[top:top + side, left:left + side]


def angles_directory(encodings_path, label, when=None):
    """A new folder for one person's generation run, next to the other face data."""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return Path(encodings_path).parent / AI_ANGLES_DIR / f"{safe_label(label)}_{stamp}"


def generate_angle_file(config, source_jpeg, angle, directory):
    """Generate one angle and save it straight away; generated images cost money."""
    image = square_image(generate_image(config, source_jpeg, angle_prompt(angle)))
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{angle.key}.jpg"
        write_jpeg(path, image, quality=95)
    except OSError as exc:
        raise AIError(f"The image couldn't be saved: {exc}") from None
    return path
