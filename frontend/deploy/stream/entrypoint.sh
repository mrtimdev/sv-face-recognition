#!/bin/sh
# Runs at every start of the container. Everything it writes outside /data is thrown away with the image.
set -eu

if [ "${#VNC_PASSWORD}" -lt 8 ]; then
    echo "VNC_PASSWORD must be at least 8 characters." >&2
    exit 1
fi

umask 077
mkdir -p /data/home "$XDG_RUNTIME_DIR"
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 /tmp/sv-supervisor.sock

x11vnc -storepasswd "$VNC_PASSWORD" /tmp/sv-vnc-password >/dev/null 2>&1
unset VNC_PASSWORD   # The app and everything else started below don't need it.

# The app keeps its files beside its code, so the code is laid into the data volume. A new image
# therefore updates the program and leaves settings and attendance as they were.
rm -rf /data/face_attendance /data/license_plate
cp -R /opt/sv-code/. /data/

exec supervisord -c /opt/sv-stream/supervisord.conf
