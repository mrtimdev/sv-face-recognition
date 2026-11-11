#!/bin/sh
# --start begins the camera and recording as soon as someone is signed in, so a restart needs nobody.
until xdpyinfo >/dev/null 2>&1; do sleep 0.5; done
cd /data
exec python dashboard.py --start
