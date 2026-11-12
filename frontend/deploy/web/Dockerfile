FROM python:3.12-slim-trixie

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 \
    SV_FACE_DATA_DIR=/data SV_FACE_HEALTH_FILE=/data/engine-health.json

RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0t64 libgl1 libegl1 libdbus-1-3 libxkbcommon0 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/sv-face
COPY requirements.txt requirements-web.txt ./
RUN sed -i 's/^opencv-python==/opencv-python-headless==/' requirements.txt \
    && pip install -r requirements-web.txt

COPY dashboard.py ./
COPY face_attendance ./face_attendance
RUN useradd --uid 1000 --create-home sv && install -d -o sv -g sv /data
USER sv

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"
ENTRYPOINT ["python", "dashboard.py", "--web", "--host", "0.0.0.0"]
