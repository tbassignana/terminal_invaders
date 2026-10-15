"""Validated local profiles and atomic JSON writes; no I/O during simulation."""

import json
import os
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .model import DIFFICULTIES, MODES, RULESET, WIDTH, Config

PROFILE_VERSION = 2
LEGACY_RULESET = "campaign-1"  # every version 1 score was played under these rules
RULESETS = (RULESET,)  # scores under other rulesets are written back untouched, never ranked or shown
SCORE_FIELDS = ("mode", "difficulty", "seed", "score", "wave", "date", "victory")
OWN_KEYS = ("version", "name", "scores", "stats", "achievements")  # any other top-level key is kept verbatim
MAX_BYTES = 2_000_000
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


def base_dir(variable: str, default: str) -> Path:
    """An XDG base directory; the specification says to ignore empty or relative values."""
    value = os.environ.get(variable, "")
    return Path(value) if os.path.isabs(value) else Path.home() / default


def data_dir() -> Path:
    base = (
        Path.home() / "Library/Application Support"
        if sys.platform == "darwin"
        else base_dir("XDG_DATA_HOME", ".local/share")
    )
    return base / "terminal-invaders"


def profile_path() -> Path:
    """Version 2 profiles have a name of their own: 1.0 replaces any profile.json it cannot read."""
    return data_dir() / "profile-2.json"


def legacy_profile_path() -> Path:
    """1.0's profile: imported until the first version 2 save, never written by this build."""
    return data_dir() / "profile.json"


def state_dir() -> Path:
    """Diagnostics belong with per-user state, never in the working directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Logs/terminal-invaders"
    return base_dir("XDG_STATE_HOME", ".local/state") / "terminal-invaders"


def log_path() -> Path:
    return state_dir() / "invaders.log"


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


def set_aside(path: Path, name: str, move: bool = False) -> Path:
    """Copy (or move) path to the sibling name, else name.1, name.2, ...; never replaces an existing file."""
    blob = None if move else path.read_bytes()
    for attempt in range(100):
        target = path.with_name(f"{name}.{attempt}" if attempt else name)
        try:
            if move:
                os.link(path, target)
                path.unlink()
            else:
                with open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                    try:
                        stream.write(blob)
                        stream.flush()
                        os.fsync(stream.fileno())
                    except OSError:
                        target.unlink(missing_ok=True)
                        raise
            return target
        except FileExistsError:
            continue
    raise FileExistsError(f"no unused name for {name}")


def decode(blob: bytes) -> tuple[dict, int]:
    """A profile object and its version; ValueError when blob is not a profile."""
    raw = json.loads(blob.decode("utf-8"))
    version = raw.get("version") if isinstance(raw, dict) else None
    if not natural(version) or version < 1:
        raise ValueError("unsupported profile format")
    return raw, version


def portable(value: object) -> bool:
    """Whether value survives a strict JSON write unchanged."""
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, RecursionError):
        return False
    return True


def clean_name(name: str) -> str:
    return "".join(c for c in str(name).upper() if c.isascii() and (c.isalnum() or c in "_-"))[:10] or "PILOT"


def natural(value: object) -> bool:
    return type(value) is int and 0 <= value <= 10**15


def board_key(config: Config | dict) -> tuple:
    fields = asdict(config) if isinstance(config, Config) else config
    seed = fields["seed"] if fields["mode"] == "daily" else None
    return fields.get("ruleset", RULESET), fields["mode"], fields["difficulty"], seed


class Profile:
    def __init__(self, path: Path | None = None):
        """The default profile starts from 1.0's profile.json; an explicit path is used exactly as given."""
        self.path = Path(path) if path is not None else profile_path()
        self.legacy_path = legacy_profile_path() if path is None else None
        self.notice, self._held = None, False
        self.data = self._read()

    def _read(self) -> dict:
        """Load known rows and keep everything else verbatim; never replace a file this build cannot read."""
        self.error = self.alert = self.unreadable = None
        self.read_only = self.migrating = False
        self.kept = {"keys": {}, "stats": {}, "scores": [], "achievements": []}
        data = {
            "version": PROFILE_VERSION,
            "name": "PILOT",
            "scores": [],
            "stats": dict.fromkeys(STAT_KEYS, 0),
            "achievements": [],
        }
        try:
            with self.path.open("rb") as stream:
                seen = os.fstat(stream.fileno())
                blob = None if seen.st_size > MAX_BYTES else stream.read()
        except FileNotFoundError:
            return self._import(data)
        except OSError as exc:
            return self._refuse(data, f"Profile could not be read ({exc})", "profile could not be read")
        if blob is None:
            # This build never writes a file this large, so it is another build's or not a profile: leave it be.
            return self._refuse(data, "Profile is larger than 2 MB", "profile is larger than 2 MB")
        try:
            raw, version = decode(blob)
        except (ValueError, RecursionError) as exc:
            # Moved aside only just before the first write, so commands that only read leave it where it is.
            self.unreadable = (seen.st_ino, seen.st_size, seen.st_mtime_ns), str(exc)
            self.error = (
                f"Profile could not be loaded ({exc}); it will be moved aside, never overwritten, once a run is saved"
            )
            self.alert = "Profile unreadable: starting fresh, the old file is kept"
            return data
        if version > PROFILE_VERSION:
            # Show what can be read, but never replace a file this build does not understand.
            self._refuse(
                data, "Profile was written by a newer version of Terminal Invaders", "profile is from a newer version"
            )
        # Version 1 here means an explicit --profile: 1.0's default file is only ever imported, never written.
        self.migrating = version == 1
        return self._parse(raw, version, data)

    def _import(self, data: dict) -> dict:
        """With no profile of its own yet, start from 1.0's; any problem with that file just means starting fresh."""
        try:
            if self.legacy_path is None or self.legacy_path.stat().st_size > MAX_BYTES:
                return data
            raw, version = decode(self.legacy_path.read_bytes())
        except (OSError, ValueError, RecursionError):
            return data
        return self._parse(raw, version, data) if version <= PROFILE_VERSION else data

    def _parse(self, raw: dict, version: int, data: dict) -> dict:
        data["name"] = clean_name(raw.get("name", "PILOT"))
        self.kept["keys"] = {k: v for k, v in raw.items() if k not in OWN_KEYS and portable(v)}
        stats = raw.get("stats", {})
        if isinstance(stats, dict):
            data["stats"].update({k: stats[k] for k in STAT_KEYS if natural(stats.get(k))})
            self.kept["stats"] = {k: v for k, v in stats.items() if k not in STAT_KEYS and portable(v)}
        unlocked = raw.get("achievements", [])
        if isinstance(unlocked, list):
            data["achievements"] = [k for k in ACHIEVEMENTS if k in unlocked]
            self.kept["achievements"] = [k for k in unlocked if isinstance(k, str) and k not in ACHIEVEMENTS]
        scores = raw.get("scores", [])
        for entry in scores if isinstance(scores, list) else ():
            if not isinstance(entry, dict):
                continue
            ruleset = LEGACY_RULESET if version == 1 else entry.get("ruleset")
            if ruleset in RULESETS and self._valid_score(entry):
                # Fields another build added ride along with the row.
                row = {k: v for k, v in entry.items() if k in SCORE_FIELDS or portable(v)}
                row.update(ruleset=ruleset, name=clean_name(entry.get("name", "PILOT")))
                data["scores"].append(row)
            elif portable(entry):
                # Another build's board, or a row this build cannot rank: written back verbatim, never shown.
                self.kept["scores"].append({**entry, "ruleset": ruleset} if version == 1 else entry)
        return data

    def _refuse(self, data: dict, problem: str, consequence: str) -> dict:
        """From here on the file on disk is left exactly as it is."""
        self.read_only = True
        self.error = f"{problem}; this session's scores will not be saved"
        self.alert = f"Scores not saved: {consequence}"
        return data

    def _move_unreadable(self) -> None:
        """Under the lock, just before the first write: move the unreadable file aside, unless it was replaced."""
        identity, problem = self.unreadable
        try:
            current = self.path.stat()
        except FileNotFoundError:
            self.unreadable = None
            return
        if (current.st_ino, current.st_size, current.st_mtime_ns) != identity:
            raise ValueError("it changed on disk after it was read")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        moved = set_aside(self.path, f"{self.path.name}.corrupt-{stamp}", move=True)
        self.unreadable, alert = None, f"Bad profile moved to {moved.name}"
        self.notice = (
            f"Profile could not be loaded ({problem}); moved it to {moved}",
            alert if len(alert) <= WIDTH else "Bad profile moved aside; its new name is shown on exit",
        )

    @contextmanager
    def _lock(self):
        """Hold the advisory profile lock where supported, yielding whether it is held; re-entrant."""
        if self._held:
            yield True
            return
        try:
            import fcntl

            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.with_suffix(".lock").open("a")
        except (ImportError, OSError):
            stream = None
        if stream is None:
            yield False
            return
        with stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX)
                self._held = True
            except OSError:
                pass
            try:
                yield self._held
            finally:
                self._held = False

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
        """Write this build's data and everything kept from other builds; a read-only profile is never touched."""
        if self.read_only:
            return False
        kept = self.kept
        payload = {
            **self.data,
            **kept["keys"],
            "stats": {**self.data["stats"], **kept["stats"]},
            "scores": [*self.data["scores"], *kept["scores"]],
            "achievements": [*self.data["achievements"], *kept["achievements"]],
        }
        try:
            if len(json.dumps(payload, separators=(",", ":"), allow_nan=False)) >= MAX_BYTES:
                raise ValueError("it would exceed 2 MB")
            with self._lock():
                if self.unreadable:
                    self._move_unreadable()
                elif self.migrating and self.path.exists():
                    # Keep the version 1 original before its first version 2 replacement.
                    set_aside(self.path, self.path.with_suffix(".v1.json.bak").name)
                atomic_json(self.path, payload)
        except (OSError, ValueError) as exc:
            self.error = f"Profile could not be saved: {exc}"
            self.alert = "Profile could not be saved; details shown on exit"
            return False
        self.migrating = False
        self.error, self.alert = self.notice or (None, None)
        return True

    def record(self, result: dict, config: Config, name: str) -> list[str]:
        """Merge with the file on disk, under a lock where supported; a read-only profile records nothing."""
        if self.read_only:
            return []
        with self._lock():
            if self.path.exists():
                previous = self.data, self.kept
                self.data = self._read()
                if self.read_only:
                    return []
                if self.unreadable:
                    # Unreadable since it was loaded, or from the start: keep what this session last saw.
                    self.data, self.kept = previous
            return self._record(result, config, name)

    def _record(self, result: dict, config: Config, name: str) -> list[str]:
        self.data["name"] = clean_name(name)
        stats = self.data["stats"]
        stats["runs"] += 1
        for key in ("kills", "shots", "hits", "ticks"):
            stats[key] += result[key]
        stats["best_combo"] = max(stats["best_combo"], result["best_combo"])
        entry = {k: result[k] for k in ("score", "wave", "victory")}
        entry.update(
            asdict(config), ruleset=RULESET, name=self.data["name"], date=datetime.now(timezone.utc).isoformat()
        )
        ranked = sorted([*self.data["scores"], entry], key=lambda r: r["score"], reverse=True)
        # Ten per board; retain the most recent 31 daily challenges. Kept rows are never pruned.
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
