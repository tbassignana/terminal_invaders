"""Developer and install scripts do what they promise; skipped where the sdist does not ship them."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def shipped(name):
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not shipped in the sdist")
    return path


def test_setup_rebuilds_a_venv_left_by_an_older_python(tmp_path):
    (tmp_path / "setup.sh").write_text(shipped("setup.sh").read_text())
    old = tmp_path / "python3.10"
    old.write_text("#!/bin/sh\nexit 1\n")
    old.chmod(0o755)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").symlink_to(old)
    # pip is left out to stay offline and fast; only which interpreter the venv runs matters here.
    chosen = tmp_path / "chosen-python"
    chosen.write_text(
        f'#!/bin/sh\nif [ "$1 $2" = "-m venv" ]; then shift 2; exec "{sys.executable}" -m venv --without-pip "$@"; fi\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    chosen.chmod(0o755)
    subprocess.run(["sh", "setup.sh"], cwd=tmp_path, env={**os.environ, "PYTHON": str(chosen)}, capture_output=True)
    venv_python = tmp_path / ".venv/bin/python"
    assert os.path.realpath(venv_python) != os.path.realpath(old)
    version = subprocess.run([venv_python, "-c", "import sys; print(sys.version_info[:2])"], capture_output=True)
    assert version.stdout.decode().strip() == str(sys.version_info[:2])


def test_make_install_provides_what_make_build_runs():
    makefile = shipped("Makefile").read_text()
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    assert "\tpython3 -m pip install -e '.[dev]'\n" in makefile and "\tpython3 -m build\n" in makefile
    assert "terminal-invaders[test]" in extras["dev"] and any(need.startswith("build") for need in extras["dev"])
