# Build from the repository root, so the image can take the desktop app's face models:
#   docker build -f verifier/Dockerfile -t sv-face-verifier .
FROM python:3.12-slim-trixie
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY verifier/requirements.txt ./
RUN pip install -r requirements.txt
# Only the face code and models the verifier uses, not the whole desktop app.
COPY frontend/face_attendance/__init__.py frontend/face_attendance/face_backend.py \
     frontend/face_attendance/geometry.py ./face_attendance/
COPY frontend/face_attendance/assets/recognition ./face_attendance/assets/recognition
COPY verifier/sv_face_verifier ./sv_face_verifier
RUN useradd --uid 1000 --create-home verifier
USER verifier
EXPOSE 8091
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/healthz', timeout=3).close()"
ENTRYPOINT ["python", "-m", "sv_face_verifier.server", "--host", "0.0.0.0"]
