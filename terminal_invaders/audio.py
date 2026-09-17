"""Optional macOS audio: tiny generated effects and only processes we own."""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

_PLAYER = Path("/usr/bin/afplay")
_RATE = 22050
# Priority order; start frequency, end frequency, duration, minimum repeat gap.
_CUES = {
    "game_over": (330, 35, 0.4, 0.5),
    "damage": (220, 45, 0.24, 0.25),
    "bomb": (95, 25, 0.32, 0.4),
    "victory": (330, 1320, 0.4, 0.5),
    "boss": (90, 330, 0.3, 0.4),
    "life": (440, 1760, 0.25, 0.3),
    "wave": (220, 880, 0.22, 0.4),
    "pickup": (660, 1320, 0.14, 0.15),
    "shield": (1320, 660, 0.1, 0.12),
    "kill": (320, 65, 0.1, 0.08),
    "hit": (420, 220, 0.04, 0.12),
    "ufo": (180, 480, 0.2, 0.4),
    "fire": (1040, 260, 0.06, 0.12),
}


def _synthesize(path: Path, start: int, end: int, duration: float) -> None:
    """Write a short, fading pulse-wave chirp with no bundled media assets."""
    samples = []
    for index in range(int(_RATE * duration)):
        elapsed = index / _RATE
        phase = math.tau * (start * elapsed + (end - start) * elapsed**2 / (2 * duration))
        envelope = min(1, elapsed / 0.003) * (1 - elapsed / duration) ** 1.8
        samples.append(int((1 if math.sin(phase) >= 0 else -1) * 9000 * envelope))
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, _RATE, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


class Audio:
    """Call update once per frame with newly emitted simulation events."""

    def __init__(self, enabled: bool = True, music: Path | None = None) -> None:
        self._available = sys.platform == "darwin" and _PLAYER.is_file()
        self.enabled = bool(enabled and self._available)
        self._closed = False
        self._effects: list[subprocess.Popen] = []
        self._music_process: subprocess.Popen | None = None
        self._music = None
        if music is not None:
            try:
                candidate = Path(music).expanduser().resolve()
                if candidate.is_file():
                    self._music = candidate
            except OSError:
                pass
        self._directory: tempfile.TemporaryDirectory | None = None
        self._last: dict[str, float] = {}

    def toggle(self) -> bool:
        """Toggle available audio, returning its new state."""
        if not self._closed:
            self.enabled = self._available and not self.enabled
            if not self.enabled:
                self._stop()
        return self.enabled

    def update(self, events: list[str], playing: bool = True) -> None:
        if not self.enabled:
            return
        try:
            if not self._reap():
                self.close()
                return
            if not playing and self._music_process is not None:
                self._stop_process(self._music_process)
                self._music_process = None
            if playing and self._music is not None and self._music_process is None:
                self._music_process = self._play(self._music, 0.3)
            now = time.monotonic()
            for cue, (start, end, duration, gap) in _CUES.items():
                if len(self._effects) >= 3:
                    break
                if cue not in events or now - self._last.get(cue, -math.inf) < gap:
                    continue
                if self._directory is None:
                    self._directory = tempfile.TemporaryDirectory(prefix="invaders-audio-")
                path = Path(self._directory.name) / f"{cue}.wav"
                if not path.exists():
                    _synthesize(path, start, end, duration)
                self._effects.append(self._play(path, 0.55))
                self._last[cue] = now
        except (OSError, ValueError, wave.Error, subprocess.SubprocessError):
            # Audio is decoration: an unavailable device must never end a game.
            self.close()

    @staticmethod
    def _play(path: Path, volume: float) -> subprocess.Popen:
        return subprocess.Popen(
            [str(_PLAYER), "-v", str(volume), str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    def _reap(self) -> bool:
        success = True
        active = []
        for process in self._effects:
            status = process.poll()
            if status is None:
                active.append(process)
            elif status != 0:
                success = False
        self._effects = active
        if self._music_process is not None:
            status = self._music_process.poll()
            if status is not None:
                self._music_process = None
                success = success and status == 0
        return success

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait()

    def _stop(self) -> None:
        for process in self._effects:
            self._stop_process(process)
        self._effects.clear()
        if self._music_process is not None:
            self._stop_process(self._music_process)
            self._music_process = None

    def close(self) -> None:
        """Reap owned players before removing their input files; safe to repeat."""
        self.enabled = False
        self._closed = True
        self._stop()
        if self._directory is not None:
            try:
                self._directory.cleanup()
            except OSError:
                pass
            self._directory = None
