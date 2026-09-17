"""Command-line entry point; terminal access happens only after validation."""

import argparse
import logging
import secrets
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .model import DIFFICULTIES, MODES, Config
from .replay import Playback
from .storage import Profile, clean_name


def bounded_int(low: int, high: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("expected an integer") from exc
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number

    return parse


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Terminal Invaders — defend Earth, one wave at a time.")
    modes = command.add_mutually_exclusive_group()
    modes.add_argument("--mode", choices=MODES, default="classic")
    modes.add_argument("--endless", dest="mode", action="store_const", const="endless", help="endless survival")
    modes.add_argument("--daily", dest="mode", action="store_const", const="daily", help="today's shared UTC seed")
    modes.add_argument("--two-player", action="store_true", help="alternating turns on the same seed")
    command.add_argument("--difficulty", choices=tuple(DIFFICULTIES), default="normal")
    command.add_argument("--seed", type=bounded_int(0, 10**15), help="repeat a specific invasion")
    command.add_argument("--name", type=clean_name, help="leaderboard pilot name (up to 10 characters)")
    command.add_argument("--fps", type=bounded_int(15, 120), default=60, help="render rate; simulation stays at 60 Hz")
    command.add_argument("--show-fps", action="store_true")
    command.add_argument("--mono", action="store_true", help="monochrome display")
    command.add_argument("--reduced-motion", action="store_true", help="reduce visual effects")
    command.add_argument("--quickstart", action="store_true", help="skip the title screen")
    command.add_argument("--no-sound", action="store_true", help="mute effects and music")
    command.add_argument("--no-music", action="store_true", help="disable custom music")
    command.add_argument("--music", type=Path, help="optional local soundtrack played by macOS afplay")
    tapes = command.add_mutually_exclusive_group()
    tapes.add_argument("--record", type=Path, metavar="FILE", help="save the last played run as a verified replay")
    tapes.add_argument("--replay", type=Path, metavar="FILE", help="watch a version 1 replay")
    tapes.add_argument("--verify-replay", type=Path, metavar="FILE", help="verify a replay without a terminal")
    command.add_argument("--profile", type=Path, help="use a separate profile JSON file")
    command.add_argument("--scores", action="store_true", help="print scores for the selected mode and exit")
    command.add_argument("--debug", action="store_true", help="write diagnostic errors to invaders.log")
    command.add_argument("--version", action="version", version=f"Terminal Invaders {__version__}")
    return command


def main(argv: list[str] | None = None) -> int:
    command = parser()
    options = command.parse_args(argv)
    options.fixed_seed = options.seed is not None or options.mode == "daily"
    if options.mode == "daily":
        options.seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
        options.difficulty = "normal"
    elif options.seed is None:
        options.seed = secrets.randbelow(10**15)
    if options.debug:
        logging.basicConfig(filename="invaders.log", level=logging.DEBUG)
    try:
        replay_path = options.verify_replay or options.replay
        replay = Playback(replay_path) if replay_path else None
        if options.verify_replay:
            from .engine import Game

            game = Game(replay.config)
            for tick in range(replay.ticks):
                game.step(replay.actions_at(tick))
                if game.tick != tick + 1:
                    raise ValueError("replay continues after the run ended")
            verified = replay.verify(game)
            print(f"{'VERIFIED' if verified else 'MISMATCH'}  {game.tick} ticks  score {game.score}  wave {game.wave}")
            return 0 if verified else 1
        profile = Profile(options.profile)
        options.name = options.name or profile.data["name"]
        if options.scores:
            from .storage import board_key

            config = Config(options.difficulty, options.mode, options.seed)
            scores = [r for r in profile.data["scores"] if board_key(r) == board_key(config)]
            print(
                f"{options.mode.upper()} / {options.difficulty.upper()}"
                + (f" / {options.seed}" if options.mode == "daily" else "")
            )
            for rank, row in enumerate(sorted(scores, key=lambda r: r["score"], reverse=True)[:10], 1):
                print(f"{rank:2}. {row['name']:<10} {row['score']:>9}  wave {row['wave']}")
            if not scores:
                print("No scores yet. Your next run starts the board.")
            if profile.error:
                print(profile.error, file=sys.stderr)
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            command.error("play in an interactive terminal; use --verify-replay FILE for headless playback")
        if options.music:
            options.music = options.music.expanduser().resolve()
            if not options.music.is_file():
                command.error("--music must point to an existing local audio file")
        from .audio import Audio
        from .ui import run

        def terminate(signum, frame):
            raise SystemExit(128 + signum)

        audio = Audio(enabled=not options.no_sound, music=None if options.no_music else options.music)
        handlers = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGHUP)}
        try:
            import curses

            try:
                run(options, profile, audio, replay)
            except curses.error as exc:
                raise ValueError(f"terminal could not initialize: {exc}") from exc
        finally:
            audio.close()
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        if profile.error:
            print(profile.error, file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        logging.debug("Unable to run", exc_info=True)
        print(f"invaders: {exc}", file=sys.stderr)
        return 1
