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
from typing import NamedTuple

from .model import DIFFICULTIES, MODES, WIDTH, Config
from .rules import LEGACY, ruleset_for
from .rules import RULESETS as GAMES
from .settings import READ_ONLY, UNREADABLE, is_default

PROFILE_VERSION = 2
LEGACY_RULESET = "campaign-1"  # every version 1 score was played under these rules
RULESETS = tuple(GAMES)  # what this version plays: its boards rank and show
KNOWN = (*RULESETS, *LEGACY)  # rows it loads (campaign-1's rank apart, on the OLD RULES page); others kept verbatim
OLD_RULES = "Scores from the 12-wave campaign (old rules)."  # the OLD RULES page's line
SCORE_FIELDS = ("mode", "difficulty", "seed", "score", "wave", "date", "victory")
OWN_KEYS = ("version", "name", "scores", "stats", "achievements", "settings")  # any other is kept verbatim
MAX_BYTES = 2_000_000
# Profiles this build writes are 3 levels deep. A deeper file is not read: the save writes with json.dump, whose
# encoder takes a Python call per level (the limit is 1000, less the game's own calls), and each Python's decoder
# stops somewhere else (near 1,000 levels on 3.11, 10,000 on 3.12-3.13, 50,000 on 3.14-3.15).
MAX_DEPTH = 100
TOO_DEEP = f"nested more than {MAX_DEPTH} levels deep"
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


def too_deep(value: object) -> bool:
    """Whether value nests lists or objects more than MAX_DEPTH levels deep (value itself is level 1); no recursion."""
    stack = [(value, 1)]
    while stack:
        item, level = stack.pop()
        for child in item.values() if isinstance(item, dict) else item if isinstance(item, list) else ():
            if isinstance(child, (dict, list)):
                if level == MAX_DEPTH:
                    return True
                stack.append((child, level + 1))
    return False


def decode(blob: bytes) -> tuple[dict, int]:
    """A profile object and its version; ValueError when blob is not a profile, or nests deeper than MAX_DEPTH: the
    same error on every Python, whether its decoder stops first or this check does."""
    try:
        raw = json.loads(blob.decode("utf-8"))
    except RecursionError:
        raise ValueError(TOO_DEEP) from None
    if too_deep(raw):
        raise ValueError(TOO_DEEP)
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


class Placing(NamedTuple):
    """Where the run record() last saved landed (Phase 5 spec 6.3): its rank on its board then (1-10, or None when
    it did not place), the board (board_key), the row's ISO date, which with the board and the score is the row's
    identity, and the best score on that board before the run (None when the board was empty)."""

    rank: int | None
    board: tuple
    date: str
    best_before: int | None
    score: int


def board_key(config: Config | dict) -> tuple:
    """The board a run or a score row belongs to: its ruleset (the one its mode plays, unless the row names one),
    mode and difficulty, and the day for daily runs."""
    fields = asdict(config) if isinstance(config, Config) else config
    seed = fields["seed"] if fields["mode"] == "daily" else None
    return fields.get("ruleset", ruleset_for(fields["mode"])), fields["mode"], fields["difficulty"], seed


class Profile:
    last_placing: Placing | None = None  # where the run record() last saved landed; None after any other record()

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
        # settings: [the value] when the file's "settings" is not an object, kept as it is and never replaced
        self.kept = {"keys": {}, "stats": {}, "scores": [], "achievements": [], "settings": []}
        data = {
            "version": PROFILE_VERSION,
            "name": "PILOT",
            "scores": [],
            "stats": dict.fromkeys(STAT_KEYS, 0),
            "achievements": [],
            "settings": {},  # the raw object, every key whose value is portable; settings.py interprets it
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
        settings = raw.get("settings", {})
        if isinstance(settings, dict):  # portability per key: a NaN drops that key, never the whole object
            data["settings"] = {k: v for k, v in settings.items() if portable(v)}
        elif portable(settings):
            self.kept["settings"] = [settings]  # another build's shape: the defaults apply, and it stays as it is
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
            if ruleset in KNOWN and self._valid_score(entry):
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
        """The HUD's HI: the best score on the board config plays (campaign-2 or Arcade; never campaign-1's)."""
        key = board_key(config)
        return max((r["score"] for r in self.data["scores"] if board_key(r) == key), default=0)

    def old_boards(self) -> dict:
        """The OLD RULES page (§17): campaign-1 rows, best first, per (mode, difficulty) in MODES and DIFFICULTIES
        order, ten each; empty when there are none, and then the page isn't shown."""
        boards = {}
        for mode in MODES:
            for difficulty in DIFFICULTIES:
                rows = [
                    row
                    for row in self.data["scores"]
                    if row["ruleset"] in LEGACY and row["mode"] == mode and row["difficulty"] == difficulty
                ]
                if rows:
                    boards[mode, difficulty] = sorted(rows, key=lambda r: r["score"], reverse=True)[:10]
        return boards

    @property
    def settings(self) -> dict:
        """A copy of the profile's raw "settings" object (Phase 5 spec 4.3), or {} when it has none or it is not an
        object; settings.merge() interprets it, so unknown keys and invalid values are in it, and kept."""
        return dict(self.data["settings"])

    @property
    def settings_unreadable(self) -> bool:
        """The file's "settings" is not an object: the defaults apply and no setting can be saved over it."""
        return bool(self.kept["settings"])

    def update_settings(self, changes: dict, name: str | None = None) -> bool:
        """Under the lock, re-read the file (as record() does), replace or delete only the keys in changes (a value
        at its built-in default, or None, deletes its key), set the pilot name when given, and save, only if that
        changes anything (Phase 5 spec 4.3). True when the file holds every change; False with an alert when it
        cannot: a read-only profile (the changes still apply to this session), or a "settings" that is not an
        object (kept as it is). Nothing to change: True, and nothing written. An unreadable file is moved aside, and
        a version 1 one backed up, by save()."""
        if self.read_only:
            changed = self._change(changes, name, settings=True)
            if changed:
                self.alert = READ_ONLY
            return not changed
        with self._lock():
            if not self._reread():  # a newer build replaced the file meanwhile
                self._change(changes, name, settings=True)
                self.alert = READ_ONLY
                return False
            # over a "settings" that is not an object, only a key with a value to write changes anything
            blocked = self.settings_unreadable and any(
                v is not None and not is_default(k, v) for k, v in changes.items()
            )
            saved = self.save() if self._change(changes, name, settings=not blocked) else True
        if blocked:
            self.alert = UNREADABLE
        return saved and not blocked

    def _change(self, changes: dict, name: str | None, *, settings: bool) -> bool:
        """Apply the changes to this session's data; whether anything changed."""
        before = dict(self.data["settings"]), self.data["name"]
        if settings:
            for key, value in changes.items():
                if value is None or is_default(key, value):
                    self.data["settings"].pop(key, None)
                else:
                    self.data["settings"][key] = value
        if name is not None:
            self.data["name"] = clean_name(name)
        return (dict(self.data["settings"]), self.data["name"]) != before

    def _reread(self) -> bool:
        """Under the lock: the file as it is now, as record() merges with it; False when it is read-only now (a newer
        build replaced it). A file that became unreadable keeps what this session last saw."""
        if self.path.exists():
            previous = self.data, self.kept
            self.data = self._read()
            if self.read_only:
                return False
            if self.unreadable:
                self.data, self.kept = previous
        return True

    def _row(self, placing: Placing) -> dict | None:
        """The placing's row, by its identity: board, date and score."""
        for row in self.data["scores"]:
            if row.get("date") == placing.date and row["score"] == placing.score and board_key(row) == placing.board:
                return row
        return None

    def rank(self, placing) -> int | None:
        """The placing's row's current rank (1-10) on its board, as this session last saw the file (its own runs
        and every run record() merged in), or None when it is gone or placing is not a Placing (spec 6.3)."""
        if not isinstance(placing, Placing):
            return None
        found = self._row(placing)
        board = [row for row in self.data["scores"] if board_key(row) == placing.board]
        board.sort(key=lambda row: row["score"], reverse=True)  # stable: as record() ranks a board
        return next((rank for rank, row in enumerate(board[:10], 1) if row is found), None)

    def rename(self, placing, name: str, pilot: bool = True) -> bool:
        """Under the lock, re-read the file, find the placing's row by (date, board, score) and set its name, and the
        profile's name when pilot, and save (spec 6.3). False, saving nothing, when the row is gone (another session
        pruned it meanwhile), the profile is read-only or placing is not a Placing."""
        if self.read_only or not isinstance(placing, Placing):
            return False
        with self._lock():
            if not self._reread():
                return False
            row = self._row(placing)
            if row is None:
                return False
            row["name"] = clean_name(name)
            if pilot:
                self.data["name"] = row["name"]
            return self.save()

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
        if kept["settings"]:
            payload["settings"] = kept["settings"][0]
        elif not payload["settings"]:
            del payload["settings"]  # nothing set: the file stays as a Phase 4 build writes it
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

    def record(self, result: dict, config: Config, name: str, *, pilot: bool = True) -> list[str]:
        """Merge with the file on disk, under a lock where supported; a read-only profile records nothing. The names
        of the achievements unlocked come back whether or not the save worked; last_placing says where the row
        landed, and only after a save that worked (else None). pilot=False (a duel's rows) leaves the profile's
        name as it is; the row still carries name."""
        self.last_placing = None
        if self.read_only:
            return []
        with self._lock():
            if not self._reread():
                return []
            return self._record(result, config, name, pilot)

    def _record(self, result: dict, config: Config, name: str, pilot: bool = True) -> list[str]:
        if pilot:
            self.data["name"] = clean_name(name)
        stats = self.data["stats"]
        stats["runs"] += 1
        for key in ("kills", "shots", "hits", "ticks"):
            stats[key] += result[key]
        stats["best_combo"] = max(stats["best_combo"], result["best_combo"])
        entry = {k: result[k] for k in ("score", "wave", "victory")}
        entry.update(
            asdict(config),
            ruleset=ruleset_for(config.mode),
            name=clean_name(name),
            date=datetime.now(timezone.utc).isoformat(),
        )
        board = board_key(entry)
        best_before = max((row["score"] for row in self.data["scores"] if board_key(row) == board), default=None)
        ranked = sorted([*self.data["scores"], entry], key=lambda r: r["score"], reverse=True)
        # Ten per board; retain the most recent 31 daily challenges of each ruleset. Kept rows are never pruned.
        dates = {}
        for row in ranked:
            if row["mode"] == "daily":
                dates.setdefault(row["ruleset"], {})[row["seed"]] = True
        dates = {ruleset: sorted(seeds, reverse=True)[:31] for ruleset, seeds in dates.items()}
        counts = Counter()
        self.data["scores"] = []
        for row in ranked:
            counts[board_key(row)] += 1
            if counts[board_key(row)] <= 10 and (row["mode"] != "daily" or row["seed"] in dates[row["ruleset"]]):
                self.data["scores"].append(row)
        unlocked = [
            k for k, (_, check) in ACHIEVEMENTS.items() if k not in self.data["achievements"] and check(result, stats)
        ]
        self.data["achievements"].extend(unlocked)
        if self.save():
            ranked = [row for row in self.data["scores"] if board_key(row) == board]  # kept in ranked order
            rank = next((n for n, row in enumerate(ranked, 1) if row is entry), None)
            self.last_placing = Placing(rank, board, entry["date"], best_before, entry["score"])
        return [ACHIEVEMENTS[k][0] for k in unlocked]
