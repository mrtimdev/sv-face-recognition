# SV Face Verifier

A small private service that SV HRM calls before it saves a profile photo or face
angle. It turns an image into SFace face vectors, so HRM can refuse a face that
already belongs to another employee. It uses the desktop app's face code and
models (`frontend/face_attendance/face_backend.py`), so HRM and the Live Monitor
always agree.

- `GET /healthz`: ready once the models have loaded.
- `POST /v1/faces/encode`: image bytes in, `{"model", "vectors"}` out. Needs the
  `X-Face-Verifier-Key` header.

## Run locally

From this folder, with a shared key of at least 32 characters:

```bash
python -m pip install -r requirements.txt
```

```bash
SV_FACE_VERIFIER_KEY=<shared key> PYTHONPATH=.:../frontend python -m sv_face_verifier.server
```

It listens on `127.0.0.1:8091`. Give SV HRM the same key as
`FACE_VERIFICATION_KEY` and `FACE_VERIFICATION_URL=http://127.0.0.1:8091`.

## Docker

Build from the repository root (the image needs the desktop app's models):

```bash
docker build -f verifier/Dockerfile -t sv-face-verifier .
```

```bash
docker run -d --name sv-face-verifier --restart unless-stopped -e SV_FACE_VERIFIER_KEY=<shared key> -p 127.0.0.1:8091:8091 sv-face-verifier
```

To run it next to SV HRM on its server, use the SV HRM repository's
`docker-compose.face-verification.yml` with `SV_FACE_SOURCE_DIR` set to this
repository's root. For a separate HTTPS host, see [DEPLOY.md](DEPLOY.md).

## Tests

```bash
python -m pip install -r requirements-test.txt
```

```bash
python -m pytest
```
