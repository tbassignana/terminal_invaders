"""Developer and install scripts do what they promise; skipped where the sdist does not ship them."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from fnmatch import fnmatch
from importlib import resources
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def shipped(name):
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not shipped in the sdist")
    return path


def run_setup(folder: Path) -> None:
    """setup.sh in a scratch folder with this interpreter. pip is left out of the venv to stay offline and fast, so
    the install step fails; only what happens before it is checked."""
    (folder / "setup.sh").write_text(shipped("setup.sh").read_text())
    chosen = folder / "chosen-python"
    chosen.write_text(
        f'#!/bin/sh\nif [ "$1 $2" = "-m venv" ]; then shift 2; exec "{sys.executable}" -m venv --without-pip "$@"; fi\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    chosen.chmod(0o755)
    subprocess.run(["sh", "setup.sh"], cwd=folder, env={**os.environ, "PYTHON": str(chosen)}, capture_output=True)


def test_setup_rebuilds_a_venv_left_by_an_older_python(tmp_path):
    old = tmp_path / "python3.10"
    old.write_text("#!/bin/sh\nexit 1\n")
    old.chmod(0o755)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").symlink_to(old)
    run_setup(tmp_path)
    venv_python = tmp_path / ".venv/bin/python"
    assert os.path.realpath(venv_python) != os.path.realpath(old)
    version = subprocess.run([venv_python, "-c", "import sys; print(sys.version_info[:2])"], capture_output=True)
    assert version.stdout.decode().strip() == str(sys.version_info[:2])


def test_setup_clears_an_earlier_build_so_files_since_removed_stay_out_of_the_install(tmp_path):
    stale = tmp_path / "build/lib/terminal_invaders/removed.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("")
    run_setup(tmp_path)
    assert not (tmp_path / "build").exists()


def test_make_install_provides_what_make_build_runs():
    makefile = shipped("Makefile").read_text()
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    assert "\t$(PYTHON) -m pip install -e '.[dev]'\n" in makefile and "\t$(PYTHON) -m build\n" in makefile
    assert "PYTHON ?= python3\n" in makefile  # what $(PYTHON) is when make is run with no variables
    assert "terminal-invaders[test]" in extras["dev"] and any(need.startswith("build") for need in extras["dev"])


def test_every_package_ships_in_the_wheel_and_the_sdist():
    """setuptools' find takes the directories with an __init__.py that match pyproject's include patterns: every
    directory of modules under terminal_invaders, the arcade among them, must be one; the sdist adds every test."""
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"]["find"]["include"]
    folders = {path.parent for path in (ROOT / "terminal_invaders").rglob("*.py")}
    names = {".".join(folder.relative_to(ROOT).parts): folder for folder in folders}
    assert {"terminal_invaders", "terminal_invaders.arcade", "terminal_invaders.term"} <= set(names)
    assert {"terminal_invaders.campaign", "terminal_invaders.legacy"} <= set(names)  # Phase 4
    for name, folder in names.items():
        assert (folder / "__init__.py").exists() and any(fnmatch(name, pattern) for pattern in patterns), name
    manifest = (ROOT / "MANIFEST.in").read_text().splitlines()
    assert "recursive-include tests *.py" in manifest and "graft tests/golden" in manifest
    # Phase 4: the bots are a package under tools/, so the sdist takes tools/ whole and test_bots.py runs from it
    assert "recursive-include tools *.py" in manifest and (ROOT / "tools/bots/__init__.py").exists()


def test_an_older_python_gets_a_sentence_not_a_traceback(monkeypatch):
    """Stock macOS python3 is 3.9: the package's first module says what to do before any 3.11 syntax is read."""
    import ast

    for name in ("invaders.py", "terminal_invaders/__main__.py", "terminal_invaders/__init__.py"):
        if (ROOT / name).exists():  # the sdist leaves out invaders.py, the checkout's launcher
            ast.parse((ROOT / name).read_text(), feature_version=(3, 6))  # what an old python3 reads first
    source = (ROOT / "terminal_invaders/__init__.py").read_text()
    monkeypatch.setattr(sys, "version_info", (3, 9, 6, "final", 0))
    monkeypatch.setattr(sys, "version", "3.9.6 (default, Aug  9 2025, 14:24:13) [Clang 17.0.0]")
    with pytest.raises(SystemExit) as stopped:
        exec(compile(source, "__init__.py", "exec"), {"__name__": "terminal_invaders"})
    assert str(stopped.value).startswith("Terminal Invaders needs Python 3.11 or newer, but this is Python 3.9.6.")
    assert "python3.13 invaders.py" in str(stopped.value)


def test_the_demo_tapes_are_package_data_and_attract_is_the_module():
    """Phase 5 spec 5.1: the title demo's tapes sit in terminal_invaders/demos/ (package data, no __init__.py), not
    in attract/, which beside attract.py would be a namespace package of the same name."""
    package_data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
    for name in ("campaign.json", "arcade.json"):
        assert any(fnmatch(f"demos/{name}", pattern) for pattern in package_data["terminal_invaders"])
        assert (resources.files("terminal_invaders") / "demos" / name).is_file()
    assert not (ROOT / "terminal_invaders/demos/__init__.py").exists()
    assert not (ROOT / "terminal_invaders/attract").exists()
    spec = importlib.util.find_spec("terminal_invaders.attract")
    assert spec.origin == str(ROOT / "terminal_invaders/attract.py") and spec.submodule_search_locations is None
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert "demos" not in manifest  # package-data puts them in the sdist too: no MANIFEST.in line


@pytest.mark.slow
def test_the_wheel_and_the_sdist_carry_the_demo_tapes_and_an_install_loads_them(tmp_path):
    """A real build through setuptools' backend, in a copy of the tree (a build writes build/ and *.egg-info next
    to its sources); then the wheel's files, unpacked as an install lays them out, load both tapes. Needs
    setuptools in this interpreter (the build tool's own isolated env would need the network)."""
    pytest.importorskip("setuptools")
    source, out, installed = tmp_path / "src", tmp_path / "dist", tmp_path / "site"
    ignore = shutil.ignore_patterns("__pycache__", "*.egg-info", "build", "dist", ".venv", ".git")
    shutil.copytree(ROOT, source, ignore=ignore)
    build = (
        "import sys; from pathlib import Path; from setuptools import build_meta as b; out, names = sys.argv[1:]; "
        "built = [b.build_wheel(out), b.build_sdist(out)]; Path(names).write_text(' '.join(built))"
    )
    names = tmp_path / "built.txt"  # the backend prints its log on stdout and rewrites sys.argv
    done = subprocess.run([sys.executable, "-B", "-c", build, out, names], cwd=source, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]
    wheel, sdist = names.read_text().split()
    wanted = {"terminal_invaders/demos/campaign.json", "terminal_invaders/demos/arcade.json"}
    with zipfile.ZipFile(out / wheel) as archive:
        assert wanted <= set(archive.namelist())
        assert "terminal_invaders/attract.py" in archive.namelist()
        archive.extractall(installed)
    with tarfile.open(out / sdist) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    assert wanted <= names and "tools/make_attract.py" in names
    for name in wanted:
        assert (installed / name).read_bytes() == (ROOT / name).read_bytes()
    load = (
        "import sys; sys.path.insert(0, sys.argv[1]); from terminal_invaders import tapes; "
        "print([playback.ruleset for playback in tapes.attract_tapes()])"
    )
    shown = subprocess.run(
        [sys.executable, "-I", "-B", "-c", load, installed], cwd=tmp_path, capture_output=True, text=True
    )
    assert shown.stdout.strip() == "['campaign-2', 'arcade-1978-1']", shown.stderr[-2000:]
