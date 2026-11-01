#!/bin/sh
# Launch Recomp Tools with the bundled virtual environment.
cd "$(dirname "$0")" && exec .venv/bin/python app/main.py "$@"
