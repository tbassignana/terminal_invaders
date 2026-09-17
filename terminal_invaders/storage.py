"""Validated local profiles and atomic JSON writes; no I/O during simulation."""

import json
import os
import sys
import tempfile
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .model import DIFFICULTIES, MODES, Config

STAT_KEYS = ("runs", "kills", "shots", "hits", "ticks", "best_combo")
ACHIEVEMENTS = {
    "first_blood": ("First contact", lambda r, s: r["kills"] >= 1),
    "combo_master": ("Chain reaction", lambda r, s: r["best_combo"] >= 5),
    "sharpshooter": ("Sharpshooter", lambda r, s: r["shots"] >= 20 and r["hits"] >= r["shots"] * 0.75),
    "survivor": ("Deep space", lambda r, s: r["wave"] >= 8),
    "high_roller": ("High roller", lambda r, s: r["score"] >= 10000),
    "veteran": ("Veteran", lambda r, s: s["runs"] >= 10),
    "defender": ("Earth defender", lambda r, s: r["victory"]),
    "centurion": ("Centurion", lambda r, s: s["kills"] >= 1000),
}


def profile_path() -> Path:
    base = (
        Path.home() / "Library/Application Support"
        if sys.platform == "darwin"
        else Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    )
    return base / "terminal-invaders/profile.json"


def atomic_json(path: Path, data: dict) -> None:
    """Replace only after a complete, flushed write on the same filesystem."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def clean_name(name: str) -> str:
    return "".join(c for c in str(name).upper() if c.isascii() and (c.isalnum() or c in "_-"))[:10] or "PILOT"


def natural(value: object) -> bool:
    return type(value) is int and 0 <= value <= 10**15


def board_key(config: Config | dict) -> tuple:
    fields = asdict(config) if isinstance(config, Config) else config
    return fields["mode"], fields["difficulty"], fields["seed"] if fields["mode"] == "daily" else None


class Profile:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else profile_path()
        self.error = None
        self.data = self._read()

    def _read(self) -> dict:
        self.error = None
        data = {"version": 1, "name": "PILOT", "scores": [], "stats": dict.fromkeys(STAT_KEYS, 0), "achievements": []}
        try:
            if self.path.stat().st_size > 2_000_000:
                raise ValueError("profile is too large")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise ValueError("unsupported profile format")
            data["name"] = clean_name(raw.get("name", "PILOT"))
            stats = raw.get("stats", {})
            if isinstance(stats, dict):
                data["stats"].update({k: stats[k] for k in STAT_KEYS if natural(stats.get(k))})
            unlocked = raw.get("achievements", [])
            if isinstance(unlocked, list):
                data["achievements"] = [k for k in ACHIEVEMENTS if k in unlocked]
            scores = raw.get("scores", [])
            if isinstance(scores, list):
                for entry in scores[:2000]:
                    if self._valid_score(entry):
                        row = {k: entry[k] for k in ("mode", "difficulty", "seed", "score", "wave", "date", "victory")}
                        row["name"] = clean_name(entry.get("name", "PILOT"))
                        data["scores"].append(row)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            self.error = f"Profile could not be loaded: {exc}"
        return data

    @staticmethod
    def _valid_score(entry: object) -> bool:
        return (
            isinstance(entry, dict)
            and entry.get("mode") in MODES
            and entry.get("difficulty") in tuple(DIFFICULTIES)
            and all(natural(entry.get(k)) for k in ("seed", "score", "wave"))
            and isinstance(entry.get("date"), str)
            and type(entry.get("victory")) is bool
        )

    def best(self, config: Config) -> int:
        key = board_key(config)
        return max((r["score"] for r in self.data["scores"] if board_key(r) == key), default=0)

    def save(self) -> bool:
        try:
            # Retain an unreadable original for recovery before replacing it.
            if self.error and self.path.exists():
                backup = self.path.with_suffix(".json.bak")
                if not backup.exists():
                    backup.write_bytes(self.path.read_bytes())
            atomic_json(self.path, self.data)
            self.error = None
            return True
        except OSError as exc:
            self.error = f"Profile could not be saved: {exc}"
            return False

    def record(self, result: dict, config: Config, name: str) -> list[str]:
        """Merge under a file lock where supported, falling back to a local save."""
        import fcntl

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock = self.path.with_suffix(".lock").open("a")
        except OSError:
            return self._record(result, config, name)
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
            except OSError:
                return self._record(result, config, name)
            if self.path.exists():
                self.data = self._read()
            return self._record(result, config, name)

    def _record(self, result: dict, config: Config, name: str) -> list[str]:
        self.data["name"] = clean_name(name)
        stats = self.data["stats"]
        stats["runs"] += 1
        for key in ("kills", "shots", "hits", "ticks"):
            stats[key] += result[key]
        stats["best_combo"] = max(stats["best_combo"], result["best_combo"])
        entry = {k: result[k] for k in ("score", "wave", "victory")}
        entry.update(asdict(config), name=self.data["name"], date=datetime.now(timezone.utc).isoformat())
        ranked = sorted([*self.data["scores"], entry], key=lambda r: r["score"], reverse=True)
        # Ten per board; retain the most recent 31 daily challenges.
        dates = sorted({r["seed"] for r in ranked if r["mode"] == "daily"}, reverse=True)[:31]
        counts = Counter()
        self.data["scores"] = []
        for row in ranked:
            counts[board_key(row)] += 1
            if counts[board_key(row)] <= 10 and (row["mode"] != "daily" or row["seed"] in dates):
                self.data["scores"].append(row)
        unlocked = [
            k for k, (_, check) in ACHIEVEMENTS.items() if k not in self.data["achievements"] and check(result, stats)
        ]
        self.data["achievements"].extend(unlocked)
        self.save()
        return [ACHIEVEMENTS[k][0] for k in unlocked]
