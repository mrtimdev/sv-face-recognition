FROM python:3.12-slim-trixie
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-verifier.txt ./
RUN pip install -r requirements-verifier.txt
COPY face_attendance ./face_attendance
RUN useradd --uid 1000 --create-home verifier
USER verifier
EXPOSE 8091
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/healthz', timeout=3).close()"
ENTRYPOINT ["python", "-m", "face_attendance.verification_server", "--host", "0.0.0.0"]
