"""Versioned, sparse input tapes, one per pilot, verified against the gameplay state digest."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__
from .engine import Game
from .model import ACTIONS, DIFFICULTIES, MODES, RULESET, Config
from .storage import atomic_json, natural

FORMAT_VERSION = 2
MAX_TICKS = 60 * 60 * 4 * 60
MAX_BYTES = 64_000_000
MAX_TAPES = 8
RESULT_KEYS = ("score", "wave", "kills", "victory")
LABEL = re.compile(r"[\w.+-]{1,64}", re.ASCII)  # ruleset and engine names are printed in error messages


class Recorder:
    def __init__(self, config: Config, pilot: int = 1):
        self.config = config
        self.pilot = pilot
        self.ticks = 0
        self.inputs = []

    def append(self, actions: tuple[str, ...]) -> None:
        if actions and self.ticks < MAX_TICKS:
            self.inputs.append([self.ticks, sorted(set(actions))])
        self.ticks += 1

    def tape(self, game) -> dict:
        if game.tick != self.ticks:
            raise ValueError("recording and simulation ticks differ")
        if self.ticks > MAX_TICKS:
            raise ValueError("replays are limited to four hours")
        result = game.result()
        return {
            "pilot": self.pilot,
            "config": asdict(self.config),
            "ticks": self.ticks,
            "inputs": self.inputs,
            "digest": game.digest(),
            "result": {key: result[key] for key in RESULT_KEYS},
        }

    def save(self, path: Path, game) -> None:
        save_tapes(path, [self.tape(game)])


def save_tapes(path: Path, tapes: list[dict]) -> None:
    """Write every pilot's tape into one replay file."""
    atomic_json(Path(path), {"format": FORMAT_VERSION, "ruleset": RULESET, "engine": __version__, "tapes": tapes})


@dataclass(frozen=True, slots=True)
class Tape:
    pilot: int
    config: Config
    ticks: int
    inputs: dict
    digest: str
    result: dict

    def actions_at(self, tick: int) -> tuple[str, ...]:
        return self.inputs.get(tick, ())

    def simulate(self) -> Game:
        """Replay headlessly; a run that ends before the tape does stops there and fails verification."""
        game = Game(self.config)
        for tick in range(self.ticks):
            game.step(self.actions_at(tick))
            if game.tick != tick + 1:
                break
        return game

    def verify(self, game) -> bool:
        result = game.result()
        return (
            game.tick == self.ticks
            and game.digest() == self.digest
            and all(result[key] == self.result[key] for key in RESULT_KEYS)
        )


class Playback:
    def __init__(self, path: Path):
        path = Path(path).expanduser()
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("replay exceeds 64 MB")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._load(raw)
        except (KeyError, TypeError, UnicodeError, RecursionError) as exc:
            raise ValueError("invalid replay structure") from exc

    def _load(self, raw: dict) -> None:
        if not isinstance(raw, dict):
            raise ValueError("invalid replay structure")
        if "format" not in raw and raw.get("version") == 1:
            raise ValueError("version 1 replays came from an older engine and cannot be verified")
        if type(raw.get("format")) is not int or raw["format"] != FORMAT_VERSION:
            raise ValueError(f"unsupported replay version (this build reads format {FORMAT_VERSION})")
        if set(raw) != {"format", "ruleset", "engine", "tapes"}:
            raise ValueError("invalid replay structure")
        ruleset, self.engine = raw["ruleset"], raw["engine"]
        if any(not isinstance(label, str) or not LABEL.fullmatch(label) for label in (ruleset, self.engine)):
            raise ValueError("invalid replay header")
        if ruleset != RULESET:
            raise ValueError(f"recorded with ruleset {ruleset} (engine {self.engine}); this version plays {RULESET}")
        tapes = raw["tapes"]
        if not isinstance(tapes, list) or not 1 <= len(tapes) <= MAX_TAPES:
            raise ValueError("invalid replay tapes")
        self.tapes = [self._tape(tape, pilot) for pilot, tape in enumerate(tapes, 1)]

    @staticmethod
    def _tape(raw: dict, pilot: int) -> Tape:
        if not isinstance(raw, dict) or set(raw) != {"pilot", "config", "ticks", "inputs", "digest", "result"}:
            raise ValueError("invalid replay tape")
        if type(raw["pilot"]) is not int or raw["pilot"] != pilot:
            raise ValueError("replay pilots must be numbered 1, 2, ... in order")
        config = raw["config"]
        if (
            not isinstance(config, dict)
            or set(config) != {"difficulty", "mode", "seed"}
            or config["difficulty"] not in DIFFICULTIES
            or config["mode"] not in MODES
            or not natural(config["seed"])
        ):
            raise ValueError("invalid replay settings")
        ticks, digest, result = raw["ticks"], raw["digest"], raw["result"]
        if not natural(ticks) or ticks > MAX_TICKS:
            raise ValueError("invalid replay duration")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid replay checksum")
        if (
            not isinstance(result, dict)
            or set(result) != set(RESULT_KEYS)
            or not all(natural(result[key]) for key in RESULT_KEYS[:3])
            or type(result["victory"]) is not bool
        ):
            raise ValueError("invalid replay result")
        inputs = raw["inputs"]
        if not isinstance(inputs, list) or len(inputs) > ticks:
            raise ValueError("invalid replay inputs")
        actions_by_tick = {}
        previous = -1
        for row in inputs:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("invalid input entry")
            tick, actions = row
            if not natural(tick) or not previous < tick < ticks:
                raise ValueError("replay ticks must be unique, increasing and in range")
            if not isinstance(actions, list) or not 1 <= len(actions) <= 32:
                raise ValueError("invalid action count")
            if any(not isinstance(a, str) or a not in ACTIONS for a in actions):
                raise ValueError("unknown replay action")
            actions_by_tick[tick] = tuple(actions)
            previous = tick
        return Tape(pilot, Config(**config), ticks, actions_by_tick, digest, dict(result))
