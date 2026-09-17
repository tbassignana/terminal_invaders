#!/bin/sh
# Install into a private environment without modifying shell startup files.
set -eu
cd -- "$(dirname -- "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install .
printf '\nInstalled. Run: %s/.venv/bin/invaders\n' "$PWD"
printf 'Or activate in zsh: source "%s/.venv/bin/activate"\n' "$PWD"
