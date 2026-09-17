"""Versioned, sparse input tapes verified against the complete simulation state."""

import json
import re
from dataclasses import asdict
from pathlib import Path

from .model import ACTIONS, DIFFICULTIES, MODES, Config
from .storage import atomic_json, natural

FORMAT_VERSION = 1
MAX_TICKS = 60 * 60 * 4 * 60
MAX_BYTES = 64_000_000


class Recorder:
    def __init__(self, config: Config):
        self.config = config
        self.ticks = 0
        self.inputs = []

    def append(self, actions: tuple[str, ...]) -> None:
        if actions and self.ticks < MAX_TICKS:
            self.inputs.append([self.ticks, sorted(set(actions))])
        self.ticks += 1

    def save(self, path: Path, game) -> None:
        if game.tick != self.ticks:
            raise ValueError("recording and simulation ticks differ")
        if self.ticks > MAX_TICKS:
            raise ValueError("replays are limited to four hours")
        atomic_json(
            Path(path),
            {
                "version": FORMAT_VERSION,
                "config": asdict(self.config),
                "ticks": self.ticks,
                "inputs": self.inputs,
                "digest": game.digest(),
            },
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
        if not isinstance(raw, dict) or raw.get("version") != FORMAT_VERSION:
            raise ValueError("unsupported replay version (use a version 1 recording)")
        config = raw["config"]
        if (
            not isinstance(config, dict)
            or set(config) != {"difficulty", "mode", "seed"}
            or config["difficulty"] not in DIFFICULTIES
            or config["mode"] not in MODES
            or not natural(config["seed"])
        ):
            raise ValueError("invalid replay settings")
        self.config = Config(**config)
        self.ticks = raw["ticks"]
        self.digest = raw["digest"]
        if not natural(self.ticks) or self.ticks > MAX_TICKS:
            raise ValueError("invalid replay duration")
        if not isinstance(self.digest, str) or not re.fullmatch(r"[a-f0-9]{64}", self.digest):
            raise ValueError("invalid replay checksum")
        inputs = raw["inputs"]
        if not isinstance(inputs, list) or len(inputs) > self.ticks:
            raise ValueError("invalid replay inputs")
        self.inputs = {}
        previous = -1
        for row in inputs:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("invalid input entry")
            tick, actions = row
            if not natural(tick) or not previous < tick < self.ticks:
                raise ValueError("replay ticks must be unique, increasing and in range")
            if not isinstance(actions, list) or not 1 <= len(actions) <= 32:
                raise ValueError("invalid action count")
            if any(not isinstance(a, str) or a not in ACTIONS for a in actions):
                raise ValueError("unknown replay action")
            self.inputs[tick] = tuple(actions)
            previous = tick

    def actions_at(self, tick: int) -> tuple[str, ...]:
        return self.inputs.get(tick, ())

    def verify(self, game) -> bool:
        return game.tick == self.ticks and game.digest() == self.digest
