#!/bin/sh
until xdpyinfo >/dev/null 2>&1; do sleep 0.5; done
exec x11vnc -display :99 -localhost -rfbport 5900 -rfbauth /tmp/sv-vnc-password \
    -forever -shared -noxdamage -noxrecord -ncache 0 -repeat -quiet
