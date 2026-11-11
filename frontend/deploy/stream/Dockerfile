# The Desktop App, unchanged, on a virtual screen you open in a browser.
# Build from frontend/:  docker compose -f deploy/stream/compose.yaml build
FROM python:3.12-slim-trixie

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# Screen and browser access, what Qt needs to draw, and fonts for Khmer names.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc openbox novnc websockify supervisor x11-utils procps tzdata \
        libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
        libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 libxkbcommon0 libdbus-1-3 \
        libgl1 libegl1 libglib2.0-0t64 libfontconfig1 libpulse0 libnss3 \
        fonts-dejavu-core fonts-khmeros \
    && rm -rf /var/lib/apt/lists/*

# opencv-python brings its own Qt, which stops PyQt6 opening a window on Linux. The dashboard never
# uses OpenCV's windows, so the headless build of the same version replaces it, whoever asked for it.
COPY requirements.txt /tmp/req/
RUN pip install -r /tmp/req/requirements.txt \
    && pip uninstall -y opencv-python opencv-python-headless \
    && pip install --no-deps opencv-python-headless==4.10.0.84 \
    && rm -rf /tmp/req

# Only the program. Dockerfile.dockerignore keeps settings, sign-ins, attendance and faces out.
COPY dashboard.py alert.wav icon.png /opt/sv-code/
COPY face_attendance /opt/sv-code/face_attendance
COPY license_plate /opt/sv-code/license_plate
COPY deploy/stream/supervisord.conf deploy/stream/openbox-rc.xml /opt/sv-stream/
COPY --chmod=755 deploy/stream/entrypoint.sh deploy/stream/run-app.sh deploy/stream/run-vnc.sh /opt/sv-stream/
RUN chmod -R a+rX /opt/sv-code /opt/sv-stream   # Files keep their modes from the build computer.

# Open straight into the app, and reconnect by itself after the browser's network drops.
RUN printf '%s' '<!doctype html><meta charset="utf-8"><title>SV Face ID</title><meta http-equiv="refresh" content="0;url=vnc.html?autoconnect=true&resize=scale&reconnect=true&reconnect_delay=2000">' \
        > /usr/share/novnc/index.html

RUN useradd --create-home --uid 1000 sv && install -d -o sv -g sv /data
USER sv

ENV DISPLAY=:99 QT_QPA_PLATFORM=xcb LANG=C.UTF-8 PYTHONUNBUFFERED=1 HOME=/data/home \
    XDG_RUNTIME_DIR=/tmp/sv-runtime LIBGL_ALWAYS_SOFTWARE=1 SV_FACE_UNATTENDED=1 \
    SV_FACE_HEALTH_FILE=/data/engine-health.json
VOLUME /data
WORKDIR /data
EXPOSE 6080

# Healthy only when every part is really running: supervisorctl's own exit code accepts a crash loop.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD [ -z "$(supervisorctl -c /opt/sv-stream/supervisord.conf status 2>&1 | grep -v ' RUNNING ')" ] && python -m face_attendance.health

ENTRYPOINT ["/opt/sv-stream/entrypoint.sh"]
