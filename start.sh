#!/bin/sh
set -eu
exec gunicorn -w 1 --threads 32 --timeout 45 --bind "0.0.0.0:${PORT:-5000}" app:app
