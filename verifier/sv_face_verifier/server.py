"""Private image-to-SFace service used by SV HRM before committing face uploads."""
import asyncio
from contextlib import asynccontextmanager
import logging
import os
import secrets
import threading

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# The same models and code as the desktop app, so HRM's checks and the Live Monitor agree.
from face_attendance.face_backend import MODEL_ID, OpenCVFaceBackend

MAX_BYTES = 10 * 1024 * 1024
_MODEL_LOCK = threading.Lock()


def encode_image(backend, content):
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or max(image.shape[:2]) > 4096:
        raise ValueError("The image cannot be read or exceeds 4096 pixels.")
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    # Keep model access serialized even if the awaiting HTTP task is cancelled.
    with _MODEL_LOCK:
        boxes = backend.face_locations(rgb)
        vectors = backend.face_encodings(rgb, boxes) if boxes else []
    return {"model": MODEL_ID, "vectors": [vector.tolist() for vector in vectors]}


def create_app(key=None, backend_factory=OpenCVFaceBackend):
    secret = key if key is not None else os.environ.get("SV_FACE_VERIFIER_KEY", "")

    @asynccontextmanager
    async def lifespan(app):
        if len(secret) < 32:
            raise RuntimeError("Set SV_FACE_VERIFIER_KEY to a shared secret of at least 32 characters.")
        cv2.setNumThreads(1)
        app.state.backend = backend_factory()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "model": MODEL_ID}

    @app.post("/v1/faces/encode")
    async def encode(request: Request):
        supplied = request.headers.get("x-face-verifier-key", "")
        if not secret or not secrets.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(401, "Invalid verifier credentials.")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise HTTPException(413, "Image exceeds 10 MB.")
        if not content:
            raise HTTPException(422, "The image is empty.")
        try:
            result = await asyncio.to_thread(encode_image, request.app.state.backend, bytes(content))
        except (ValueError, cv2.error) as exc:
            raise HTTPException(422, "The image could not be verified.") from exc
        except Exception as exc:
            logging.exception("Face verification failed")
            raise HTTPException(503, "Face verification is unavailable.") from exc
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return app


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, workers=1, limit_concurrency=16)


if __name__ == "__main__":
    main()
