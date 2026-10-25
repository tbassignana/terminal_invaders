"""Command-line entry point; terminal access happens only after validation."""

import argparse
import logging
import os
import secrets
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .keymap import PRESETS, Keymap, KeymapError
from .model import DIFFICULTIES, MODES, Config
from .replay import Playback
from .settings import SETTING_DESTS, merge
from .storage import Profile, clean_name, log_path
from .term.caps import TIERS
from .term.tty import TerminalGone, open_tty
from .theme import PALETTE_NAMES

EPILOG = (
    "Keys: arrows or A/D move, Space fires, B bombs, P pauses (Q in the pause menu leaves a run), M sound. "
    "Esc Esc quits from the title, where SETTINGS and HOW TO PLAY are."
)


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


def require_terminal(command: argparse.ArgumentParser) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        command.error("play in an interactive terminal; use --verify-replay FILE for headless playback")


def require_probe_terminal(command: argparse.ArgumentParser) -> None:
    """--probe asks the terminal it runs in, on a tty fd of its own; its report may go to a file or a pipe."""
    try:
        os.close(open_tty())
    except ValueError:
        command.error("--probe asks the terminal it runs in: run it in one (its report may go to a file or a pipe)")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Terminal Invaders — defend Earth, one wave at a time.", epilog=EPILOG
    )
    modes = command.add_mutually_exclusive_group()
    modes.add_argument(
        "--mode", choices=(*MODES, "campaign"), default="classic", help="campaign is the same as classic"
    )
    modes.add_argument("--endless", dest="mode", action="store_const", const="endless", help="endless survival")
    modes.add_argument("--daily", dest="mode", action="store_const", const="daily", help="today's shared UTC seed")
    modes.add_argument("--arcade", dest="mode", action="store_const", const="arcade", help="the 1978 original's rules")
    modes.add_argument("--two-player", action="store_true", help="alternating turns on the same seed")
    command.add_argument("--difficulty", choices=tuple(DIFFICULTIES), default="normal")
    command.add_argument("--seed", type=bounded_int(0, 10**15), help="repeat a specific invasion")
    command.add_argument("--name", type=clean_name, help="leaderboard pilot name (up to 10 characters)")
    command.add_argument("--fps", type=bounded_int(15, 120), default=60, help="render rate; simulation stays at 60 Hz")
    command.add_argument(
        "--show-fps", action="store_true", help="show the stats line: fps, round trip, pacing, bytes per frame (F1)"
    )
    colors = command.add_mutually_exclusive_group()
    colors.add_argument(
        "--color",
        choices=("auto", *TIERS),
        default="auto",
        help="colour tier (auto: what the terminal supports; NO_COLOR gives mono, --color beats it)",
    )
    colors.add_argument("--mono", dest="color", action="store_const", const="mono", help="monochrome: --color mono")
    command.add_argument(
        "--theme",
        choices=("auto", "dark", "light", "terminal"),
        default="auto",
        help="the background: dark or light colours (auto: match the terminal's background; terminal: leave that "
        "background as it is)",
    )
    command.add_argument(
        "--palette",
        choices=PALETTE_NAMES,
        default="default",
        help="colours: default, cvd (colour-blind safe, red-green), contrast (high contrast) or mono; "
        "beats a saved palette for this run, and --color beats it",
    )
    command.add_argument(
        "--effects",
        choices=("full", "calm", "off"),
        default="full",
        help="sparks, shakes and stars in the Campaign modes: full, calm (no shake, still stars) or off",
    )
    scales = command.add_mutually_exclusive_group()
    scales.add_argument(
        "--scale",
        choices=("auto", "1", "2", "3", "4", "6"),
        default="auto",
        help="largest pixel scale for the arena (auto: the largest that fits; 1: text glyphs)",
    )
    scales.add_argument("--glyphs", dest="scale", action="store_const", const="1", help="text glyphs: --scale 1")
    command.add_argument(
        "--controls",
        choices=("auto", "hold", "latched", "timed"),
        default="auto",
        help="hold keys (kitty-protocol terminals), tap to move (latched), or hold inferred from key repeat (timed)",
    )
    keys = command.add_mutually_exclusive_group()
    keys.add_argument("--keys", choices=tuple(PRESETS), default="default", help="key binding preset")
    keys.add_argument("--keymap", type=Path, metavar="FILE", dest="keymap_file", help="key bindings from a JSON file")
    command.add_argument(
        "--pacing",
        choices=("auto", "adaptive", "fixed", "off"),
        default="auto",
        help="frames that may await the terminal's reply: adaptive 2 to 12 (auto), fixed 2, or off (clock pacing)",
    )
    command.add_argument("--no-kitty", action="store_true", help="never use the kitty keyboard protocol")
    command.add_argument("--no-sync", action="store_true", help="never use synchronized output")
    command.add_argument("--reduced-motion", action="store_true", help="reduce visual effects")
    command.add_argument(
        "--tips",
        choices=("auto", "always", "off"),
        default="auto",
        help="tips on the bottom row: auto (until your first Campaign, Endless or Daily run ends), always or off",
    )
    command.add_argument("--quickstart", action="store_true", help="skip the title screen")
    command.add_argument("--no-sound", action="store_true", help="start muted (M turns sound on)")
    tapes = command.add_mutually_exclusive_group()
    tapes.add_argument("--record", type=Path, metavar="FILE", help="save the last played run as a verified replay")
    tapes.add_argument("--replay", type=Path, metavar="FILE", help="watch a replay, one pilot after another")
    tapes.add_argument("--verify-replay", type=Path, metavar="FILE", help="verify a replay without a terminal")
    command.add_argument("--profile", type=Path, help="use a separate profile JSON file")
    command.add_argument("--scores", action="store_true", help="print scores for the selected mode and exit")
    command.add_argument("--probe", action="store_true", help="report what this terminal supports and exit")
    reports = command.add_mutually_exclusive_group()
    reports.add_argument("--json", action="store_true", help="with --probe: print the capabilities as JSON")
    reports.add_argument("--raw", action="store_true", help="with --probe: print a terminal fixture, to report it")
    command.add_argument(
        "--debug",
        action="store_true",
        help="append diagnostic errors to invaders.log in ~/Library/Logs/terminal-invaders on macOS, "
        "elsewhere in $XDG_STATE_HOME/terminal-invaders (default ~/.local/state/terminal-invaders)",
    )
    command.add_argument("--version", action="version", version=f"Terminal Invaders {__version__}")
    return command


def defaults(**values) -> argparse.Namespace:
    """The options main() makes of a bare command line (the default keymap, no seed), with values set over them:
    what tools and tests build a Session from. It reads no profile and no settings: every option it made before
    Phase 5 keeps its value, and of the new ones the title demo is off, so no tool or test sitting on the menu
    starts one (spec 1.2)."""
    options = parser().parse_args([])
    options.keymap, options.fixed_seed = Keymap.preset(options.keys), False
    options.attract, options.given, options.given_flags = "off", frozenset(), {}
    return argparse.Namespace(**vars(options) | values)


def given_options(argv: list[str] | None) -> frozenset[str]:
    """The settings-backed options this command line sets (spec 4.4): the same argv parsed again with their defaults
    replaced by a marker, so the ones still holding it were not given (--glyphs gives scale, --no-sound no_sound,
    --keymap keys). The marker is an object, not argparse.SUPPRESS: argparse runs a string default through the
    option's type (--fps, --name and --keymap have one), and would read "==SUPPRESS==" as a value."""
    unset = object()
    command = parser()
    command.set_defaults(**dict.fromkeys(SETTING_DESTS, unset))
    found = vars(command.parse_args(argv))
    return frozenset(option for dest, option in SETTING_DESTS.items() if found[dest] is not unset)


def given_flags(argv: list[str] | None) -> dict[str, str]:
    """The flag typed for a row whose flag has another name, by row key: {"scale": "--glyphs"} when --glyphs set
    the scale, else {}. The settings card names the flag the player typed (ST-4), not the row's own (--scale)."""
    typed = object()
    command = parser()
    next(action for action in command._actions if "--glyphs" in action.option_strings).const = typed
    return {"scale": "--glyphs"} if vars(command.parse_args(argv))["scale"] is typed else {}


def main(argv: list[str] | None = None) -> int:
    command = parser()
    options = command.parse_args(argv)
    options.given, options.given_flags = given_options(argv), given_flags(argv)
    if options.mode == "campaign":  # what players call classic; tapes, boards and the CLI keep classic (spec 16.3)
        options.mode = "classic"
    if (options.json or options.raw) and not options.probe:
        command.error("--json and --raw go with --probe")
    try:  # a keymap file is read before the profile, so its errors come first; --probe takes the flags' keymap
        custom = Keymap.load(options.keymap_file) if options.keymap_file else None
        options.keymap = custom or Keymap.preset(options.keys)
    except KeymapError as exc:
        command.error(str(exc))
    options.fixed_seed = options.seed is not None or options.mode == "daily"
    if options.mode == "daily":
        options.seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
        options.difficulty = "normal"
    elif options.seed is None:
        options.seed = secrets.randbelow(10**15)
    if options.mode == "arcade":
        options.difficulty = "normal"  # the 1978 rules have no difficulty setting
    try:
        if options.debug:
            log = log_path()
            log.parent.mkdir(parents=True, exist_ok=True)
            logging.basicConfig(filename=log, level=logging.DEBUG)
        replay_path = options.verify_replay or options.replay
        replay = Playback(replay_path) if replay_path else None
        if options.replay:
            replay.refuse_watching()  # a campaign-1 tape verifies, but it can't be shown: say so before the screen opens
        if options.verify_replay:
            verified = []
            for tape in replay.tapes:
                game = tape.simulate()
                verified.append(tape.verify(game))
                status = "VERIFIED" if verified[-1] else "MISMATCH"
                level = "round" if tape.config.mode == "arcade" else "wave"
                print(f"{status}  pilot {tape.pilot}  {game.tick} ticks  score {game.score}  {level} {game.wave}")
            return 0 if all(verified) else 1
        if options.probe:
            require_probe_terminal(command)
            from .app import run_probe_command

            return run_probe_command(options)
        profile = Profile(options.profile)
        options.name = options.name or profile.data["name"]
        if options.scores:
            from .storage import board_key
            from .ui import mode_name

            config = Config(options.difficulty, options.mode, options.seed)
            scores = [r for r in profile.data["scores"] if board_key(r) == board_key(config)]
            arcade = options.mode == "arcade"
            print(
                mode_name(options.mode)
                + ("" if arcade else f" / {options.difficulty.upper()}")
                + (f" / {options.seed}" if options.mode == "daily" else "")
            )
            for rank, row in enumerate(sorted(scores, key=lambda r: r["score"], reverse=True)[:10], 1):
                print(f"{rank:2}. {row['name']:<10} {row['score']:>9}  {'round' if arcade else 'wave'} {row['wave']}")
            if not scores:
                print("No scores yet. Your next run starts the board.")
            if profile.error:
                print(profile.error, file=sys.stderr)
            return 0
        # A flag wins for this run, then the saved setting, then the built-in default (spec 4.4); flags are never
        # saved. Everything below reads the merged options: the keymap, the sound, the Session and the Renderer.
        merge(options, profile.settings)
        options.keymap = custom or Keymap.preset(options.keys)
        require_terminal(command)
        from .audio import Audio

        def terminate(signum, frame):
            raise SystemExit(128 + signum)

        audio = Audio(enabled=not options.no_sound)
        # While the game owns the terminal, its TerminalSession handles these (and SIGINT, SIGQUIT, SIGTSTP) itself.
        handlers = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGHUP)}
        try:
            from .app import run

            run(options, profile, audio, replay)
        finally:
            audio.close()
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        if profile.error:
            print(profile.error, file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        return 130
    except TerminalGone:
        logging.debug("Terminal gone", exc_info=True)
        return 129
    except (OSError, ValueError) as exc:
        logging.debug("Unable to run", exc_info=True)
        print(f"invaders: {exc}", file=sys.stderr)
        return 1
