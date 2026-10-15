#!/bin/sh
# Install into a private environment without modifying shell startup files.
# PYTHON=python3.13 ./setup.sh picks the interpreter; it must be Python 3.11 or newer.
set -eu
cd -- "$(dirname -- "$0")"
python=${PYTHON:-python3}
if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    found=$("$python" -c 'import platform; print(platform.python_version())' 2>/dev/null) || found="not found"
    printf 'Terminal Invaders needs Python 3.11 or newer, but %s is %s.\n' "$python" "$found" >&2
    printf 'Install a newer Python, then run setup again with it:\n' >&2
    printf '  Homebrew: brew install python@3.13 && PYTHON=python3.13 ./setup.sh\n' >&2
    printf '  uv:       uv python install 3.13 && PYTHON="$(uv python find 3.13)" ./setup.sh\n' >&2
    printf '  pipx:     brew install pipx && pipx install .   (no setup.sh needed)\n' >&2
    exit 1
fi
# --clear: a venv left by an older Python keeps that interpreter even when rebuilt with another one.
"$python" -m venv --clear .venv
.venv/bin/python -m pip install .
printf '\nInstalled. Run: %s/.venv/bin/invaders\n' "$PWD"
printf 'Or activate in zsh: source "%s/.venv/bin/activate"\n' "$PWD"
