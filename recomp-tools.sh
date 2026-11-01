#!/bin/sh
# Launch Recomp Tools. On first run (fresh download), create the
# bundled virtual environment and install the app's Python deps.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    echo "First run: setting up the Python environment..."
    python3 -m venv .venv || {
        echo "Could not create the environment. On Debian/Ubuntu run:"
        echo "  sudo apt install -y python3-venv"
        echo "then run ./recomp-tools.sh again."
        exit 1
    }
    .venv/bin/pip install -r requirements.txt || {
        echo "Dependency install failed - check your connection and re-run."
        exit 1
    }
fi
exec .venv/bin/python app/main.py "$@"
