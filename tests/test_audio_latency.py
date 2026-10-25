"""Gates G2.2 and G2.3 on the real sound server (spec 11, 14 A2/A12/A13): pytest -m audio tests/test_audio_latency.py.

Each case runs tools/audio_latency.py, which plays through the real Audio client and helper on the null sink "game"
and records its monitor. Skipped unless `pactl get-default-sink` prints game. The tool's JSON is printed (-s shows
it) and is in every assertion message.
"""

import json
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "audio_latency.py"
LIMITS = {"pulse-simple": (35, 50, 60), "pw-cat": (18, 45, 55), "pacat": (18, 45, 55), "aplay": None}  # median, p90


def default_sink() -> str | None:
    if sys.platform == "darwin" or shutil.which("pactl") is None:
        return None
    try:
        return subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


pytestmark = [
    pytest.mark.audio,
    pytest.mark.skipif(default_sink() != "game", reason="needs the null sink game as the default sink"),
]


def measure(*arguments: str) -> dict:
    done = subprocess.run([sys.executable, str(TOOL), *arguments], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.splitlines()[-1])
    print(json.dumps(result))
    return result


@pytest.mark.parametrize("backend", list(LIMITS))
def test_cue_latency_on_the_null_sink(backend):
    result = measure("--backend", backend, "--runs", "3")
    assert all(run["heard"] + run["stale"] == 20 and run["stale"] <= 1 for run in result["runs"]), result
    if LIMITS[backend] is not None:  # aplay is reported only
        low, high, p90 = LIMITS[backend]
        assert low <= result["median_ms"] <= high and result["p90_ms"] <= p90, result


def test_tone_dropouts_through_pulse_simple():
    result = measure("--backend", "pulse-simple", "--runs", "3", "--tone")
    assert min(result["dropped"]) >= 0 and statistics.median(result["dropped"]) <= 6, result
