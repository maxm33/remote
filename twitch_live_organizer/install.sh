#!/usr/bin/env bash

set -euo pipefail

SCRIPT="twitch_live_organizer.py"

cd "$(dirname "$0")"

py_usable() {
    command -v py >/dev/null 2>&1 && py -3 --version >/dev/null 2>&1
}

refresh_path() {
    command -v cygpath >/dev/null 2>&1 || return 0
    local dir
    for base in "${LOCALAPPDATA:-}" "${PROGRAMFILES:-}"; do
        [ -n "$base" ] || continue
        for sub in "Programs/Python/Launcher" "Python Launcher"; do
            dir="$(cygpath -u "$base")/$sub"
            [ -d "$dir" ] && PATH="$dir:$PATH"
        done
    done
    export PATH
}

if py_usable; then
    echo "[ok] py is already installed: $(py -3 --version)"
else
    echo "[..] py not found or not usable, installing Python..."

    if ! command -v winget >/dev/null 2>&1; then
        echo "[error] winget not available. Install Python manually from" >&2
        echo "        https://www.python.org/downloads/ (tick 'py launcher')." >&2
        exit 1
    fi

    winget install -e --id Python.Python.3.12 --scope user \
        --accept-package-agreements --accept-source-agreements || true

    refresh_path

    if ! py_usable; then
        echo "[error] Python was installed but py still isn't usable here." >&2
        echo "        Close this terminal, open a new one, and run the script again." >&2
        exit 1
    fi
    echo "[ok] Installed: $(py -3 --version)"
fi

if [ ! -f "$SCRIPT" ]; then
    echo "[error] $SCRIPT not found in $(pwd)" >&2
    exit 1
fi

echo "[..] Running $SCRIPT --install-startup"
py "./$SCRIPT" --install-startup
