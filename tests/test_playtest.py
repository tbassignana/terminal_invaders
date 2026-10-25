"""tools/playtest.py and tools/playtest_grade.py (Phase 5 spec 15.3): the panel's game behind a socket, and the
grader on synthetic logs (one passing run, then each way a run fails, each with its reason code)."""

import importlib.util
import itertools
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_invaders.keymap import DEFAULT
from tests.support.playtest_baseline import ANSWERS, MISSED, TELLS

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_for_test", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


grade_tool = load("playtest_grade")
playtest = load("playtest")

GOOD = {
    "a": "A and D (or the arrow keys) move, Space fires, B drops a bomb, P pauses.",
    "b": "LIVES: how many ships you have left. GUN: shots ready to fire. BOMBS: bombs you hold. BOMB %: hits in a "
    "row fill it until you earn another bomb.",
    "c": "An invader changes shape just before it fires.",
    "d": "Pause with P, then Q goes back to the menu.",
    "e": "Nothing much; the upgrade card was a surprise.",
}


# --- synthetic logs -------------------------------------------------------------------------------------------------


class Log:
    """A hidden log written command by command, as playtest.py writes it."""

    def __init__(self, fixture="apple-terminal-26", seed=1):
        self.records = [{"header": {"fixture": fixture, "seed": seed, "cols": 80, "rows": 24, "limit": 300}}]
        self.n, self.now = 0, 1.0

    def add(self, command, scene, *, keys=None, named=True, game=None, seconds=0.4, screen=None, **extra):
        self.n += 1
        start, self.now = self.now, self.now + seconds
        words = command.split()
        sent = keys if keys is not None else (words[1:] if words[0] == "press" else [])
        truth = {"scene": scene, "context": scene, "mode": "classic", "pause_reason": "manual", "demo": False}
        truth |= {"replay": False, "pilots": 1}
        truth |= {key: extra.pop(key) for key in ("pause_reason", "shows_run", "shows_bot", "tell") if key in extra}
        if game is not None:
            truth["game"] = {"mode": "classic", "phase": "playing", "wave": 1, "lives": 3, "score": 0, "kills": 0}
            truth["game"] |= {"tick": int(self.now * 60)} | game
        record = {
            "n": self.n,
            "command": command,
            "start": round(start, 4),
            "end": round(self.now, 4),
            "keys": [{"key": key, "at": start, "named": named} for key in sent],
            "waits": [],
            "screen_sha256": screen or f"{self.n:064x}",
            "truth": truth,
        }
        record |= extra
        self.records.append(record)
        return record

    def write(self, path: Path) -> Path:
        path.write_text("".join(json.dumps(record) + "\n" for record in self.records))
        return path


def passing_run(tell: bool | None = None) -> Log:
    """Every task done. tell: the truth's "tell" (G6) on every command, True at the end of the fire command (a shooter's
    tell on screen) or False on all; None, an older log without it (its play in wave 1 showed none)."""
    log = Log()
    log.add("look", "menu")
    log.add("press enter", "playing", game={})
    log.add("hold 3 space", "playing", keys=["space"], game={"kills": 6, "score": 300})
    log.add("press p", "paused", game={"kills": 6, "score": 300})
    log.add("press p", "playing", game={"kills": 6, "score": 300})
    log.add("wait 10", "initials", game={"kills": 9, "score": 1230, "phase": "game_over"}, seconds=10)
    profile = {"scores": [{"name": "BOT", "score": 1230, "wave": 2}], "settings": {}}
    log.add("press b o t enter", "results", game={"kills": 9, "score": 1230, "phase": "game_over"}, profile=profile)
    log.add("press enter", "menu", game={"kills": 9, "score": 1230, "phase": "game_over"})
    log.add("press down down down enter", "records", shows_run=True, shows_bot=True)
    log.add("press escape", "menu")
    log.add("press down down down down enter", "settings")
    log.add("press right", "settings", profile={"scores": profile["scores"], "settings": {"palette": "cvd"}})
    log.add("press escape", "menu")
    log.add("press down enter", "menu", exited=0)
    log.records.append({"exit": 0, "at": log.now})
    for record in log.records if tell is not None else ():
        if "truth" in record:
            record["truth"]["tell"] = tell and record["n"] == 3
    return log


def graded(tmp_path, log: Log, debrief=GOOD, baseline=False) -> dict:
    path = log.write(tmp_path / "log.jsonl")
    answer = tmp_path / "debrief.json"
    if debrief is not None:
        answer.write_text("The answers:\n" + json.dumps(debrief))
    return grade_tool.grade(path, answer if debrief is not None else None, baseline)


def test_a_run_that_does_every_task_passes(tmp_path):
    result = graded(tmp_path, passing_run())
    assert result["pass"], result["reasons"]
    assert result["tasks"] == {1: 2, 2: 3, 3: 5, 4: 6, 5: 8, 6: 9, 7: 12, 8: 14}
    assert result["guesses"] == [] and result["debrief"]["e"]["confusing"].startswith("Nothing much")
    assert grade_tool.main([str(tmp_path / "log.jsonl"), str(tmp_path / "debrief.json")]) == 0


def without(log: Log, n: int) -> Log:
    log.records = [record for record in log.records if record.get("n") != n]
    return log


@pytest.mark.parametrize(
    "change,reason",
    [
        ("no kills", "task-2:stuck:menu"),
        ("never paused", "task-3:stuck:menu"),
        ("no BOT row", "task-5:stuck:menu"),
        ("records without the run", "task-6:stuck:menu"),
        ("no palette", "task-7:stuck:menu"),
        ("quit from play", "task-8:stuck:menu"),
        ("unnamed launch", "task-1:unnamed-key"),
        ("unnamed quit", "task-8:unnamed-key"),
        ("dead end", "task-2:stuck:help"),
        ("too many commands", "task-1:limit"),
        ("too long", "task-4:limit"),
        ("void", "void"),
    ],
)
def test_each_failure_has_its_reason_code(tmp_path, change, reason):
    log = passing_run()
    records = {record["n"]: record for record in log.records if "n" in record}
    if change == "no kills":
        for record in records.values():
            if "game" in record["truth"]:
                record["truth"]["game"]["kills"] = 2
    elif change == "never paused":
        records[4]["truth"]["scene"] = "playing"
    elif change == "no BOT row":
        records[7]["profile"]["scores"][0]["name"] = "PILOT"
    elif change == "records without the run":
        records[9]["truth"]["shows_bot"] = False
    elif change == "no palette":
        records[12]["profile"]["settings"] = {"palette": "contrast"}
    elif change == "quit from play":
        records[13]["truth"]["scene"] = "playing"
    elif change == "unnamed launch":
        records[2]["keys"][0]["named"] = False
    elif change == "unnamed quit":
        records[14]["keys"][1]["named"] = False
    elif change == "dead end":
        stuck = Log()
        stuck.add("look", "menu")
        stuck.add("press enter", "playing", game={})
        for _ in range(10):
            stuck.add("press x", "help", screen="ab" * 32)
        log.records = stuck.records + [record for record in log.records[1:] if record.get("n", 0) > 2]
        for number, record in enumerate((r for r in log.records if "n" in r), 1):
            record["n"] = number
    elif change == "too many commands":
        filler = Log()
        for _ in range(300):
            filler.add("look", "menu", screen=f"{filler.n % 2:064x}")
        log.records = filler.records
    elif change == "too long":
        records[6]["end"] = 26 * 60.0
    elif change == "void":
        log.records.append({"void": "command 3 (press p): the screen differs from the log"})
    result = graded(tmp_path, log)
    assert not result["pass"]
    assert reason in result["reasons"], result["reasons"]


def test_guessed_keys_are_counted_not_failed(tmp_path):
    log = passing_run()
    log.records[3]["keys"][0]["named"] = False  # the fire of task 2, which needs no named key
    result = graded(tmp_path, log)
    assert result["pass"], result["reasons"]
    assert result["guesses"] == [{"n": 3, "command": "hold 3 space", "key": "space"}]


def test_the_baseline_needs_no_initials_and_no_palette(tmp_path):
    log = passing_run()
    for record in log.records:
        if record.get("profile"):
            record["profile"] = {"scores": [{"name": "PILOT", "score": 1230}], "settings": {}}
        if record.get("n") == 9:
            record["truth"]["shows_bot"] = False
    assert not graded(tmp_path, log)["pass"]
    result = graded(tmp_path, log, baseline=True)
    assert result["pass"], result["reasons"]
    assert 7 not in result["tasks"]


@pytest.mark.parametrize(
    "question,answer",
    [
        ("a", "Arrows move and Space fires."),  # no bomb, no pause
        ("a", "A bomb is B and P pauses, Space fires."),  # `A bomb` is not a move key
        ("b", "LIVES are lives. The rest are numbers."),
        ("c", "It just fires."),
        ("c", "I couldn't tell; they just fire."),  # the verb, not the noun (TL-4)
        ("c", "Hard to tell, nothing obvious."),
        ("c", "I never saw any warning!"),  # an exclamation mark is not the `!` glyph
        ("c", "No idea, it just fires!"),
        ("c", 'Bullets appear as a "!" directly under the shooting invader.'),  # the shot's `!`, not the tell's
        ("c", 'A shot only becomes visible once it exists, as a "!" under an invader.'),  # the shot turns into it (G5)
        ("c", "I saw no change in its shape before it fired."),  # a change denied
        ("c", "Nothing visibly changed: the sprite only alternated between its two frames."),
        ("d", "Press Escape."),  # pause, but not Q or the menu
    ],
)
def test_the_debrief_rubric_catches_each_wrong_answer(tmp_path, question, answer):
    result = graded(tmp_path, passing_run(tell=True), GOOD | {question: answer})  # (c) for a run that showed a tell
    assert result["reasons"] == [f"debrief-{question}"]


def test_the_debrief_rubric_takes_answers_in_other_shapes(tmp_path):
    answers = {
        "a": {"move": "left and right arrow keys", "fire": "spacebar", "bomb": "B", "pause": "Esc"},
        "b": ["ships remaining", "how many shots are ready", "bombs held in reserve", "charge toward the next bomb"],
        "c": "the alien's pose changes, a tell",
        "d": "pause it (P), then choose menu",
        "e": "",
    }
    result = graded(tmp_path, passing_run(), answers)
    assert result["pass"], result["reasons"]
    assert graded(tmp_path, passing_run(), None)["reasons"] == ["debrief-a", "debrief-b", "debrief-c", "debrief-d"]


@pytest.mark.parametrize(
    "question,answer",
    [
        ("a", "Left and right arrows move, Space fires, X drops a bomb, P pauses."),  # X is a bomb key too (TL-5)
        ("a", "H and L move, Space fires, B bombs, P pauses."),  # H and L move as well
        ("a", "Q and D move, Z fires, B bombs, Esc pauses."),
        (
            "b",
            "LIVES: ships left. GUN: how many of your two shot slots are free. BOMBS: the number of bombs you can "
            "still use. BOMB %: a charge meter for the next bomb.",
        ),
        ("c", 'The invader that is about to shoot changes one of its characters to "!" for a moment.'),
        ("c", "Just before it fires, one eye turns into an exclamation mark."),
        ("c", 'A "!" shows on the invader just before it fires.'),
        ("c", "Nothing obvious, but the invader's pose changes first."),  # the denial is another clause's
        ("c", "There is a tell: the invader's look changes."),
    ],
)
def test_the_debrief_rubric_takes_each_right_answer(tmp_path, question, answer):
    result = graded(tmp_path, passing_run(tell=True), GOOD | {question: answer})
    assert result["pass"], result["reasons"]


NO_WARNING = 'I saw no warning before a shot: the "!" is the shot itself.'
FLASHES = "the invader flashes red before it fires."
DROPS = 'A "!" drops from under the invader.'  # an observation of a shot, so (c) turns on what else it asserts


@pytest.mark.parametrize(
    "tell,answer,right",
    [
        (False, NO_WARNING, True),  # what screens with no tell show
        (False, 'A "!" drops from under the invader as the shot comes out.', True),
        (False, 'Just before it fires, its middle turns into a "!".', True),  # the pellet leaving: `\W/` to `\!/`
        (False, FLASHES.capitalize(), False),  # a warning the game does not give, asserted
        (False, f'A "!" drops from under the invader, and {FLASHES}', False),
        (False, f'A "!" drops from under the invader, and {FLASHES.replace("flashes", "may flash")}', True),  # a guess
        (False, 'My guess is a sound cue; I can\'t tell. The "!" shot just falls from under the invader.', True),
        (False, "A beep sounds just before an invader shoots.", False),
        (False, "A marker appears above the invader before it fires.", False),
        (False, "I did not see this, because I never saw a wave of invaders.", False),  # no observation (b2)
        (False, f"I can't tell; {FLASHES.replace('flashes', 'might flash')}", False),  # a guess, and no observation
        # G7: a denial reaches the rest of its list (h1, h4), up to a contrast or a colon
        (False, f"I never saw an invader change its look, blink, or show a marker before it fired. {DROPS}", True),
        (False, f"I could not spot a flash, a changed sprite or a marker on an invader before it fired. {DROPS}", True),
        (False, f"{DROPS} I saw no change of shape, but {FLASHES}", False),
        (False, f"{DROPS} I saw no change of shape, only a red flash before it fires.", False),
        (False, f"{DROPS} Nothing changed shape: {FLASHES}", False),
        # G7: a sentence that opens with a supposition asserts nothing (h6); an "if" inside one does not suppose
        (False, f"{NO_WARNING} If the warning is a colour or flash, a plain-text screen would not show it.", True),
        (False, f"{DROPS} Unless the cue is a colour, this text screen shows no warning.", True),
        (False, f"{DROPS} In case the warning is a sound, mine was off.", True),
        (False, f"{DROPS} But if the warning is a red flash, I never saw it.", True),
        (False, f"{DROPS[:-1]}, and it flashes red before it fires if you watch closely.", False),
        (True, NO_WARNING, False),  # this run's screens showed a tell
        (True, 'The invader changes shape just before it fires: its middle turns into a "!".', True),
    ],
)
def test_c_is_graded_against_what_the_runs_screens_showed(tmp_path, tell, answer, right):
    """G6: a run whose screens showed no shooter in its tell pose is right to say it saw no warning and the `!` is the
    shot, or that a `!` comes out of the invader; it is wrong with no observation of a shot, or when it asserts a
    warning the game does not give (a hedged guess or a supposition is not an assertion, and a denial reaches the
    rest of its list up to a contrast or a colon: G7). A run that showed a tell names it."""
    result = graded(tmp_path, passing_run(tell=tell), GOOD | {"c": answer})
    assert result["tells"] is tell and result["debrief"]["c"] == {"pass": right, "tells": tell}
    assert result["reasons"] == ([] if right else ["debrief-c"])


def test_a_log_without_the_tell_field_shows_none_only_before_wave_5(tmp_path):
    """An older log has no truth "tell": Campaign play before wave 5 showed none (pellets have no tell); a later
    wave, another mode or the title's demo cannot say, and (c) is then graded as if a tell showed."""
    log = passing_run()
    commands = [record for record in log.records if "truth" in record]
    assert grade_tool.tells_shown(commands) is False
    assert graded(tmp_path, log, GOOD | {"c": NO_WARNING})["pass"]
    for change in ({"wave": 5}, {"mode": "endless"}, "demo"):
        log = passing_run()
        record = log.records[3]  # the fire command
        if change == "demo":
            record["truth"]["demo"] = True
        else:
            record["truth"]["game"] |= change
        result = graded(tmp_path, log, GOOD | {"c": NO_WARNING})
        assert result["tells"] is None and result["reasons"] == ["debrief-c"], change
    log = passing_run(tell=False)
    log.records[3]["truth"]["game"]["wave"] = 5  # the field says what the screen showed, whatever the wave
    assert graded(tmp_path, log, GOOD | {"c": NO_WARNING})["pass"]


@pytest.mark.parametrize("run", sorted(ANSWERS))
def test_the_rubric_grades_the_panels_debriefs_as_read_by_hand(run):
    """The baseline's and the gate's panels' own answers (tests/support/playtest_baseline.py), (c) graded against what
    each run's screens showed: no tell on any of them. b2 never saw a wave and guessed (b) and (c); b3, b5, g5 and g6
    say there is no warning and the `!` is the shot, and so do h1-h4 and h6 (h1 and h4 deny a list of cues, h6
    supposes one); h5 sees the `!` come out of the invader; b6 got (b) right in other words."""
    result = grade_tool.grade_debrief(ANSWERS[run], TELLS[run])
    assert result["c"]["tells"] is False
    assert "".join(q for q in "abcd" if not result[q]["pass"]) == MISSED[run], result


def test_rubric_a_takes_every_letter_the_default_keymap_binds():
    """(a) is graded on the keymap's labels (spec 15.3): every letter key of DEFAULT's play context names its
    action, a left key with a right key for move."""
    play = DEFAULT["play"]
    letters = {action: [key.upper() for key in play[action] if len(key) == 1] for action in play}
    answer = "{left} and {right} move, {fire} fires, {bomb} drops a bomb, {pause} pauses."
    first = {action: keys[0] for action, keys in letters.items()}
    for action in ("left", "right", "fire", "bomb", "pause"):
        for key in letters[action]:
            text = answer.format(**(first | {action: key}))
            assert all(rule(text) for rule in grade_tool.KEYS.values()), (text, grade_tool.grade_debrief({"a": text}))
    for left, right in itertools.product(letters["left"], letters["right"]):
        assert grade_tool.KEYS["move"](f"{left} goes left and {right} goes right")


def with_key_scenes(record: dict, *scenes: str) -> dict:
    """The scene in front of each of the record's keys, as playtest.py logs it (a Campaign game's)."""
    for key, scene in zip(record["keys"], scenes, strict=True):
        key["truth"] = {"scene": scene, "pause_reason": "manual", "demo": False, "replay": False}
        key["truth"]["game"] = {"mode": "classic"}
    return record


def test_a_pause_and_resume_in_one_command_does_task_3(tmp_path):
    """`press p p` pauses and resumes inside one command: the grader reads the scene in front of each key (TL-14)."""
    log = passing_run()
    log.records = [record for record in log.records if record.get("n") != 5]  # the resume
    records = {record["n"]: record for record in log.records if "n" in record}
    records[4]["command"], records[4]["truth"]["scene"] = "press p p", "playing"
    records[4]["keys"].append(dict(records[4]["keys"][0]))
    assert 3 not in grade_tool.tasks_done([r for r in log.records if "n" in r], None)  # the command's end alone
    with_key_scenes(records[4], "playing", "paused")
    result = graded(tmp_path, log)
    assert result["pass"], result["reasons"]
    assert result["tasks"][3] == 4


def test_a_quit_through_the_menu_in_one_command_does_task_8(tmp_path):
    """`press q escape escape` from the results leaves through the menu inside one command (TL-14)."""
    log = passing_run()
    records = {record["n"]: record for record in log.records if "n" in record}
    records[13]["truth"]["scene"] = "results"  # the command before the quit does not end on the menu
    records[14]["command"] = "press q escape escape"
    records[14]["keys"] = [{"key": key, "at": 0.0, "named": True} for key in ("q", "escape", "escape")]
    assert not graded(tmp_path, log)["pass"]
    with_key_scenes(records[14], "results", "menu", "menu")
    result = graded(tmp_path, log)
    assert result["pass"], result["reasons"]
    assert result["tasks"][8] == 14
    with_key_scenes(records[14], "results", "playing", "paused")  # a quit pressed from play is not task 8
    assert "task-8:stuck:menu" in graded(tmp_path, log)["reasons"]


# --- the tool's own pieces ----------------------------------------------------------------------------------------


def test_commands_parse_and_refuse():
    assert playtest.parse("press Enter esc B") == ("press", ("enter", "escape", "b"))
    assert playtest.parse("hold 2.5 d+space") == ("hold", (2.5, ("d", "space")))
    assert playtest.parse("wait 10") == ("wait", (10.0,))
    assert playtest.parse("look") == ("look", ())
    for bad in ("", "press", "press shift", "hold 11 a", "wait 0.05", "wait x", "jump", "look now", "down a b"):
        with pytest.raises(playtest.CommandError):
            playtest.parse(bad)


def test_a_key_is_named_by_its_label_or_the_screens_words():
    hint = ["Tap A/D: move, S: stop  Space Fire  B Bomb  P Pause"]
    assert all(playtest.named(key, hint) for key in ("a", "d", "s", "space", "b", "p"))
    assert not any(playtest.named(key, hint) for key in ("q", "enter", "escape", "x", "up"))
    menu = ["Arrows select  Enter choose  Esc Esc quit"]
    assert all(playtest.named(key, menu) for key in ("up", "down", "enter", "escape"))
    assert playtest.named("t", ["Type letters for your name"]) and playtest.named("7", ["TYPE LETTERS"])
    assert playtest.named("x", ["Press any key"]) and not playtest.named("x", ["Press a key"])
    pause = ["P / Escape / Enter resume", "R restart   M sound   Q menu"]  # the pause panel writes Esc out in full
    assert all(playtest.named(key, pause) for key in ("p", "escape", "enter", "r", "m", "q"))
    assert not playtest.named("escape", ["Escapes: none"]) and not playtest.named("backspace", pause)


def test_the_truths_tell_is_a_shooter_in_its_tell_pose_on_a_screen_that_draws_play():
    """G6: Game.tell() is the Telegraph tip's test (a kind with a tell pose, in state "telegraph") in the game the
    screen draws: play, a pause, the title's demo; never a card or the too-small notice. A diver's or a mite's wobble
    before a dive is the same state, but no tell (they never fire)."""

    def shown(kind="gunner", state="telegraph", scene="playing", fits=True, demo=False):
        game = SimpleNamespace(aliens=[SimpleNamespace(kind=kind, state=state)], config=SimpleNamespace(mode="classic"))
        drawn = SimpleNamespace(scene=scene, pilots=[game], pilot=SimpleNamespace(game=game), fits=fits, attract=None)
        if demo:  # the title's demo draws its own session's game
            drawn = SimpleNamespace(scene="attract", pilots=[], fits=True, attract=drawn)
        return playtest.Game.tell(SimpleNamespace(session=drawn))

    assert shown() and shown(kind="scout") and shown(scene="paused") and shown(demo=True)
    assert not any(shown(kind=kind) for kind in ("diver", "mite"))
    assert not shown(state="formation") and not shown(fits=False)
    assert not any(shown(scene=scene) for scene in ("results", "menu", "initials", "records"))


# --- serve and send -----------------------------------------------------------------------------------------------


def quiet_env() -> dict:
    env = os.environ | {"PYTHONDONTWRITEBYTECODE": "1"}
    env.pop(playtest.SOCKET_ENV, None)
    return env


def serve_command(run: Path, sock: Path, player: Path | None = None, seed: int = 3) -> list[str]:
    command = [sys.executable, "-B", str(TOOLS / "playtest.py"), "serve", "--run", str(run), "--fixture"]
    command += ["apple-terminal-26", "--seed", str(seed), "--tree", str(ROOT)]
    return command + (["--socket", str(sock)] if player is None else ["--player-folder", str(player)])


def start_server(run: Path, sock: Path, player: Path | None = None) -> subprocess.Popen:
    """The server, on `sock` (or on the socket of the player's folder, which is then `sock`)."""
    ready = sock.parent / f"{sock.name}.ready"
    ready.unlink(missing_ok=True)
    command = serve_command(run, sock, player)
    env = quiet_env()
    server = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    deadline = time.monotonic() + 60
    while not ready.exists():
        assert server.poll() is None, server.communicate()[0]
        assert time.monotonic() < deadline, "the server did not come up"
        time.sleep(0.05)
    return server


def send(sock: Path | None, command: str, cwd: Path | None = None, code: int = 0) -> str:
    """One command, as a player sends it: with --socket, or (sock None) from the folder cwd."""
    done = subprocess.run(
        [sys.executable, "-B", str(TOOLS / "playtest.py"), "send", command, *(["--socket", str(sock)] if sock else [])],
        capture_output=True, text=True, timeout=60, cwd=cwd, env=quiet_env(),
    )  # fmt: skip
    assert done.returncode == code, done.stdout + done.stderr
    return done.stdout


def stop(server: subprocess.Popen, sock: Path) -> None:
    try:
        send(sock, "__shutdown__")
        server.wait(timeout=30)
    finally:
        if server.poll() is None:  # a server that stopped answering (a failing test) is not left running
            server.kill()
            server.wait()


@pytest.fixture
def sock():
    """A socket path short enough for AF_UNIX (tmp_path may not be), removed afterwards with its ready file."""
    path = Path(tempfile.gettempdir()) / f"playtest-test-{os.getpid()}.sock"
    yield path
    for left in (path, path.parent / f"{path.name}.ready"):
        left.unlink(missing_ok=True)


def test_serve_and_send_play_one_game_and_replay_its_log(tmp_path, sock):
    run = tmp_path / "run"
    server = start_server(run, sock)
    try:
        title = send(sock, "look")
        assert "LAUNCH" in title and "Enter choose" in title and len(title.splitlines()) <= 24
        play = send(sock, "press enter")
        assert "Space Fire" in play and "LAUNCH" not in play
        assert "PAUSED" in send(sock, "press p")
        assert "PAUSED" not in send(sock, "press p")  # a second P, after the tool's guard wait
        assert send(sock, "jump").startswith("error: not a command: jump")
    finally:
        stop(server, sock)
    records = [json.loads(line) for line in (run / "log.jsonl").read_text().splitlines()]
    commands = [record for record in records if isinstance(record.get("n"), int)]
    assert [record["command"] for record in commands] == ["look", "press enter", "press p", "press p"]
    assert [record["truth"]["scene"] for record in commands] == ["menu", "playing", "paused", "playing"]
    assert all(key["named"] for record in commands for key in record["keys"])
    assert [key["truth"]["scene"] for record in commands for key in record["keys"]] == ["menu", "playing", "paused"]
    assert commands[3]["keys"][0]["truth"]["game"]["mode"] == "classic"  # the scene each key was pressed in (TL-14)
    assert commands[3]["waits"] and commands[3]["waits"][0]["key"] == "p"  # the legacy guard's wait, logged
    assert commands[3]["keys"][0]["at"] >= commands[2]["keys"][0]["at"] + playtest.TAP + playtest.GUARD_WAIT
    assert records[-1] == {"command": "jump", "error": "not a command: jump", "n": None}
    server = start_server(run, sock)  # the log is replayed and every screen checked
    try:
        assert "PAUSED" in send(sock, "press p")
    finally:
        stop(server, sock)
    records = [json.loads(line) for line in (run / "log.jsonl").read_text().splitlines()]
    assert not any("void" in record for record in records)
    assert [record["n"] for record in records if isinstance(record.get("n"), int)] == [1, 2, 3, 4, 5]
    lines = (run / "log.jsonl").read_text().splitlines()
    planted = json.loads(lines[1])  # the first command, look
    planted["screen_sha256"] = "0" * 64
    lines[1] = json.dumps(planted)
    (run / "log.jsonl").write_text("\n".join(lines) + "\n")
    server = start_server(run, sock)
    stop(server, sock)
    records = [json.loads(line) for line in (run / "log.jsonl").read_text().splitlines()]
    assert any(record.get("void", "").startswith("command 1 (look)") for record in records)


def test_each_player_folder_reaches_its_own_game(tmp_path):
    """The panel's players send the same command line, each from its own empty folder (spec 15.3, as built here)."""
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    sock = playtest.socket_for(one)
    assert sock != playtest.socket_for(two) and len(str(sock)) < 100
    server = start_server(tmp_path / "run", sock, player=one)
    try:
        assert "LAUNCH" in send(None, "look", cwd=one)
        assert send(None, "look", cwd=two, code=1) == "The game is not running.\n"
        assert list(one.iterdir()) == [] and list(two.iterdir()) == []  # the folders stay empty
    finally:
        send(None, "__shutdown__", cwd=one)
        server.wait(timeout=30)
        gone = not sock.exists() and not (sock.parent / f"{sock.name}.ready").exists()
        for left in (sock, sock.parent / f"{sock.name}.ready"):
            left.unlink(missing_ok=True)
    assert gone  # the server takes its socket with it


def log_of(run: Path) -> list[dict]:
    return [json.loads(line) for line in (run / "log.jsonl").read_text().splitlines()]


def test_a_client_that_sends_nothing_or_leaves_before_its_reply_leaves_the_game_up(tmp_path, sock):
    """A probe that connects and closes, and a client gone before its reply, do not stop the server (TL-1)."""
    run = tmp_path / "run"
    server = start_server(run, sock)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.connect(str(sock))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as gone:
            gone.connect(str(sock))
            gone.sendall(b"wait 3\n")
        assert "LAUNCH" in send(sock, "look")
    finally:
        stop(server, sock)
    assert [record["command"] for record in log_of(run) if "command" in record] == ["wait 3", "look"]


def test_a_restart_after_a_profile_write_replays_on_an_empty_home(tmp_path, sock):
    """The replay rebuilds the profile from an empty HOME, as the first pass began on one, so a restart after the
    settings were saved replays every screen alike (TL-3)."""
    run = tmp_path / "run"
    server = start_server(run, sock)
    try:
        assert "PALETTE" in send(sock, "press escape up enter")  # Esc to QUIT, Up to SETTINGS
        send(sock, "press right")  # the next palette
        menu = send(sock, "press escape")  # saved
    finally:
        stop(server, sock)
    assert [record["profile"]["settings"] for record in log_of(run) if "profile" in record] == [{"palette": "cvd"}]
    server = start_server(run, sock)
    try:
        assert send(sock, "look") == menu
    finally:
        stop(server, sock)
    assert not any("void" in record for record in log_of(run))


def test_a_signal_stops_serve_and_takes_its_socket(tmp_path, sock):
    """SIGTERM ends serve though the game stands frozen between commands (TL-2); the next test sends ^C and SIGHUP
    while the game starts."""
    server = start_server(tmp_path / "run", sock)
    try:
        assert "Space Fire" in send(sock, "press enter")
        server.send_signal(signal.SIGTERM)
        assert server.wait(timeout=10) == 128 + signal.SIGTERM
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
    assert not sock.exists() and not (sock.parent / f"{sock.name}.ready").exists()


STARTING = """
import os, runpy, sys, time
from pathlib import Path

root, ready, window, signum = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
sys.path.insert(0, str(root))
from terminal_invaders import app
from terminal_invaders.term import tty


def signalled():
    deadline = time.monotonic() + 60
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    os.kill(os.getpid(), signum)
    time.sleep(0.2)  # its handler has run by now


if window == "cli":  # cli.main's own SIGTERM and SIGHUP handler holds: the TerminalSession is not open yet
    run = app.run

    def started(*args, **kwargs):
        signalled()
        return run(*args, **kwargs)

    app.run = started
else:  # the TerminalSession's handlers hold: it is open, and play has not begun
    opened = tty.TerminalSession.open

    def started(self):
        opened(self)
        signalled()

    tty.TerminalSession.open = started
sys.argv = [str(root / "tools" / "playtest.py"), *sys.argv[5:]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@pytest.mark.parametrize("window,signum", [("cli", signal.SIGHUP), ("session", signal.SIGINT)])
def test_a_signal_while_the_game_starts_stops_serve(tmp_path, sock, window, signum):
    """A signal just after the ready file, while the game is still starting, ends serve too: the game's own handlers
    never take one (gate G5, run 1: a ^C after the ready file, before play began, hung serve in 1 run of 12; cli.main's
    own SIGTERM and SIGHUP handler, before that, did the same). Here the game's own thread sends it, once the ready
    file is up, so it lands in that moment on every run."""
    ready = sock.parent / f"{sock.name}.ready"
    ready.unlink(missing_ok=True)
    command = [sys.executable, "-B", "-c", STARTING, str(ROOT), str(ready), window, str(int(signum))]
    command += serve_command(tmp_path / "run", sock)[3:]  # serve and its arguments
    server = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=quiet_env())
    try:
        assert server.wait(timeout=60) == 128 + signum, server.communicate()[0]
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
    assert not sock.exists() and not ready.exists()


STAGED = """
import runpy, sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from terminal_invaders import ui
from terminal_invaders.campaign.testing import staged
from terminal_invaders.model import Config

made, first = ui.make_game, [True]


def make_game(config):
    if first and config.mode == "classic":  # the first Campaign game: wave 5, whose gunners show a tell
        first.clear()
        return staged(Config("normal", "classic", 1978), 5, phase="playing", immortal=True)
    return made(config)


ui.make_game = make_game
sys.argv = [str(root / "tools" / "playtest.py"), *sys.argv[2:]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def ask(sock: Path, command: str) -> str:
    """One command, as `send` sends it, from this process."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(60)
        client.connect(str(sock))
        client.sendall(command.encode() + b"\n")
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    return data.decode()


def test_the_log_says_which_screens_show_a_tell(tmp_path, sock):
    """G6: the truth's "tell", read from a real serve log. Staged wave 5's gunners take their tell pose `(@)` before
    a dart, the first 80 ticks in. The log says "tell" for exactly the screens that show one, and the grader then
    grades (c) for a run that showed a tell."""
    run = tmp_path / "run"
    ready = sock.parent / f"{sock.name}.ready"
    ready.unlink(missing_ok=True)
    command = [sys.executable, "-B", "-c", STAGED, str(ROOT), *serve_command(run, sock)[3:]]
    server = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=quiet_env())
    shown = []
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert server.poll() is None and time.monotonic() < deadline, "the server did not come up"
            time.sleep(0.05)
        shown.append(ask(sock, "look"))
        shown.append(ask(sock, "press enter"))
        while len(shown) < 60 and not (any("(@)" in screen for screen in shown) and "(@)" not in shown[-1]):
            shown.append(ask(sock, "wait 0.1"))
    finally:
        stop(server, sock)
    commands = [record for record in log_of(run) if isinstance(record.get("n"), int)]
    assert len(commands) == len(shown)
    assert [record["truth"]["tell"] for record in commands] == ["(@)" in screen for screen in shown]
    told = [record for record in commands if record["truth"]["tell"]]
    assert told and all(record["truth"]["game"]["wave"] == 5 for record in told)
    assert commands[0]["truth"]["tell"] is False and commands[-1]["truth"]["tell"] is False
    debrief = tmp_path / "debrief.json"
    debrief.write_text(json.dumps(GOOD | {"c": NO_WARNING}))
    result = grade_tool.grade(run / "log.jsonl", debrief)
    assert result["tells"] is True and "debrief-c" in result["reasons"]


def test_serve_refuses_a_log_it_cannot_replay_and_keeps_its_home(tmp_path, sock):
    """Another run's log (a wrong --seed), or one with a line cut short, ends serve with a message, not a hang, and
    leaves the run's HOME as it was (TL-9)."""
    run = tmp_path / "run"
    stop(start_server(run, sock), sock)
    kept = run / "home" / "kept"
    kept.write_text("the run's profile\n")

    def refused(seed: int) -> str:
        done = subprocess.run(serve_command(run, sock, seed=seed), capture_output=True, text=True, timeout=30,
                              env=quiet_env())  # fmt: skip
        assert done.returncode == 2 and not sock.exists(), done.stdout + done.stderr
        return done.stderr

    assert "not this run's log (fixture and seed differ)" in refused(4)
    with (run / "log.jsonl").open("a") as log:
        log.write('{"n": 1, "comm')  # a kill in the middle of a line
    assert "line 2 is not a log record" in refused(3)
    assert kept.exists()
