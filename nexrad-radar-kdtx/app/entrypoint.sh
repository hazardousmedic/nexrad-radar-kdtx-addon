#!/bin/sh
set -e
mkdir -p /data/output
python /app/fetch_render.py &
exec python -m http.server 8600 --directory /data/output
