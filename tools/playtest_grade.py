#!/usr/bin/env python3
"""Grade one new-player run (Phase 5 spec 15.3): python3 tools/playtest_grade.py LOG DEBRIEF [--json].

LOG is tools/playtest.py's hidden log (log.jsonl), DEBRIEF the agent's answer: a JSON object with one field per
question, a-e (a JSON object anywhere in the file, so a transcript's last message will do). The run passes when the
log shows the eight tasks done, within MAX_COMMANDS commands and MAX_MINUTES virtual minutes, with no dead end and
every key that first completed tasks 1, 3, 5, 6, 7 and 8 named on the screen in front of it, and the debrief gets
(a), (c) and (d) right and at least 3 of (b)'s 4 readouts. Each failed condition gets a reason code:
task-N:stuck:<scene>, task-N:unnamed-key, task-N:limit, debrief-<q>, void. Guessed keys (sent while the screen in
front of them did not name them) are counted and listed, never failed. (e), what was confusing, is collected.
(c), how the game warns of a shot, is graded against what the run's screens could show: the log says whether any of
them showed a shooter in its tell pose, and a run that met none (Campaign before wave 5) is right to say it saw no
warning.

The rubric is published here as keyword sets (RUBRIC) and dry-run on the baseline panel's answers before the gate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MAX_COMMANDS = 250
MAX_MINUTES = 25.0
DEAD_END = 10  # commands in a row outside play with neither the scene nor the screen changing
NAMED_TASKS = (1, 3, 5, 6, 7, 8)  # the tasks whose completing keys must have been named on screen
TASKS = {
    1: "start a game of the main mode",
    2: "shoot down at least 5 invaders",
    3: "pause the game, then carry on",
    4: "play until the game is over",
    5: "initials BOT if asked, then back to the title",
    6: "find the run in the high scores",
    7: "switch to the colour-blind palette",
    8: "quit the game",
}


def words(*patterns: str) -> re.Pattern:
    return re.compile("|".join(patterns), re.IGNORECASE)


def token(*labels: str) -> re.Pattern:
    """Key labels as whole words, case kept (a lone `A` is a key; the article `a` is not)."""
    return re.compile("|".join(rf"(?<![A-Za-z0-9]){re.escape(label)}(?![A-Za-z0-9])" for label in labels))


# (a): every one of move, fire, bomb and pause named by a key the default keymap binds to it (keymap.py DEFAULT;
# test_playtest.py checks these sets against it): a left key and a right key (a lone `A` may be the article), or
# the arrows.
KEYS = {
    "move": lambda text: bool(
        (token("A", "Q", "H").search(text) and token("D", "L").search(text))
        or (words(r"\bleft\b").search(text) and words(r"\bright\b").search(text))
        or words(r"\barrows?\b", "←", "→").search(text)
    ),
    "fire": lambda text: bool(
        token("W", "Z", "K").search(text)
        or words(r"\bspace(bar)?\b", r"\bup arrow\b", r"\barrow up\b", "↑").search(text)
    ),
    "bomb": lambda text: bool(token("B", "X").search(text) or words(r"\b[bx] key\b", r"\bkey [bx]\b").search(text)),
    "pause": lambda text: bool(token("P").search(text) or words(r"\besc(ape)?\b", r"\bp key\b").search(text)),
}
# (b): LIVES, GUN, BOMBS and BOMB %: at least 3 of the 4 meanings.
READOUTS = {
    "lives": words(
        r"\b(ships?|lives|life|tries|chances)\b[^.;\n]{0,40}\b(left|remain\w*|spare|have|until|before)\b",
        r"\bhow many (ships|lives|tries)\b",
        r"\b(extra|remaining|spare) (ships|lives)\b",
    ),
    "gun": words(
        r"\bshots?\b[^.;\n]{0,40}\b(ready|available|loaded|left|free|can fire|at once|in the air|in flight|before)\b",
        r"\b(shot|gun|fire|bullet) slots?\b",
        r"\bready to fire\b",
        r"\bammo\b",
        r"\bhow many shots\b",
    ),
    "bombs": words(
        r"\bbombs?\b[^.;\n]{0,40}\b(held|hold|have|carry\w*|left|available|stock\w*|remaining|in reserve|own"
        r"|can (still )?(use|drop|fire|set off))\b",
        r"\b(how many|number of) bombs\b",
    ),
    "meter": words(
        r"\b(fill\w*|charg\w*|progress|meter|build\w* up)\b",
        r"\b(toward|towards|until) (the next |a |another )?bomb\b",
        r"\bearn\w*\b[^.;\n]{0,30}\bbomb\b",
        r"\b100 ?%",
    ),
}
# (c) is graded against what the run's screens could show (gate G6): the log says whether any screen it showed had
# a shooter in its tell pose (tells_shown). Pellets come with no tell, so a Campaign run that ends before wave 5
# shows none, and an honest answer then says it saw no warning.
#
# A run that showed a tell: (c) names it. An invader changes shape (pose, look) before it fires, or shows the `!` of
# the tell poses. A `!` counts as the tell only on the invader (it turns into one, or one shows on it before it
# fires): the falling shot is a `!` too, so these two patterns (ON_INVADER) do not count in a clause that names the
# shot or a `!` coming into view ("a shot only becomes visible once it exists, as a `!`"). "Tell" counts as the noun
# ("a tell"), not the verb ("I couldn't tell"). A match does not count in a clause that denies it ("nothing changed",
# "I saw no change in its shape").
LOOK = r"(shape|pose|look|form|appearance|sprite|characters?|glyphs?|face|eyes?)"
CHANGE = r"(chang\w*|alter(s|ed|ing)?|switch\w*|swap\w*|morph\w*)"  # not "alternates": both idle frames do
BANG = r"([\"'`‘’“”]![\"'`‘’“”]|\bexclamation\b|(?<![\w!?.])!(?![\w!?]))"
TELL = tuple(
    words(pattern)
    for pattern in (
        rf"\b{CHANGE}\b[^.;\n]{{0,30}}\b{LOOK}\b",
        rf"\b{LOOK} (\w+ )?{CHANGE}\b",
        rf"\b(replac|turn|becom|chang|switch|swap|morph)\w*\b[^.;\n]{{0,40}}?{BANG}",
        rf"{BANG}[^.;\n]{{0,30}}\b(on|in|over|above)( the| an| its| their| that)? (invader|alien|enemy|sprite|face)",
        rf"{BANG}[^.;\n]{{0,40}}\b(before|about to)\b[^.;\n]{{0,20}}\b(fir|shoot|shot)\w*",
        r"\b(a|the|its|their|this|that|visual)\s+tells?\b",
        r"\bwind(s|ing)? up\b",
    )
)
ON_INVADER = TELL[2:4]  # the `!` the invader turns into, and the `!` on it
DENIED = words(r"\b(no|not|never|nothing|none|neither|nor|without|cannot)\b", r"n[’']t\b")
SHOT = words(r"\b(shots?|bullets?|projectiles?|missiles?|visible)\b")  # ON_INVADER's `!` is then the shot's
CLAUSE = re.compile(r"[.;:,!?]\s|\n")  # where a clause starts
# A run that showed none: (c) is right when it reports what those screens showed (seen): a TELL match, ON_INVADER's
# in any clause (the only change they show is the pellet leaving the invader, `\W/` to `\!/`); a `!` in, under or
# from the invader (AT_INVADER); or that there is no warning (NO_WARNING) with the `!` named as the shot in one
# sentence. It is wrong with no such observation ("I never saw a wave"), or when it asserts as fact a warning the
# game does not give (asserted): a sentence with a CUE (a colour, a flash, a sound, a marker) and a WARN phrase tying
# it to the shot ("before it fires", "a warning"), each in a clause no denial reaches, and no HEDGE ("may",
# "probably", "my guess", "I can't tell", "if there is a cue", or a sentence that opens with "if", "unless" or "in
# case": a guess or a supposition is not an assertion). A denial reaches the later clauses of its sentence, up to a
# colon or a clause that opens with a CONTRAST: "I never saw it change its look, blink, or show a marker before it
# fired" denies all three, and "I saw no change of shape, but it flashes red before it fires" asserts. A flash in a
# clause that shows a glyph ("one flash to /!\") is the change of shape, not a colour. Sentences and clauses end at a
# `!` only after a word: a lone `!` is the glyph.
AT_INVADER = words(
    rf"{BANG}[^.;\n]{{0,40}}?\b(on|in|over|above|under|below|beneath|from|out of)\b[^.;\n]{{0,20}}?"
    r"\b(invader|alien|enemy|sprite|shooter)s?\b"
)
NO_WARNING = words(
    r"(\b(no|not|never|nothing|none|without|cannot)\b|n[’']t\b)[^.;\n]{0,60}?"
    r"\b(warn\w*|cues?|telegraph\w*|signs?|signals?|indicat\w*|alerts?)\b",
    r"\bfirst (sign|thing|warning|hint|indication|cue|clue)\b",  # "the first sign of a shot was the `!` itself"
)
CUE = words(
    r"\b(colou?r\w*|red|yellow|orange|green|blue|purple|bright\w*|glow\w*|highlight\w*|blink\w*|flicker\w*"
    r"|flash\w*|sound\w*|beep\w*|audio|audible|tones?|noises?|chimes?|alarms?|markers?|indicators?)\b"
)
FLASH = words(r"flash\w*")
WARN = words(
    r"\b(before|ahead of|preced\w*)\b[^.;\n]{0,30}\b(fir\w*|shoot\w*|shots?|attack\w*)\b",
    r"\babout to (fire|shoot|attack)\b",
    r"\b(warn\w*|telegraph\w*|pre-?fir\w*|announc\w*|signal\w*|cues?|in advance)\b",
)
HEDGE = words(
    r"\b(may|might|maybe|perhaps|probably|possibly|presumably|likely|guess\w*|suspect\w*|assum\w*|seems?"
    r"|suppos\w*|could be|would be|not (sure|certain)|unsure|uncertain|unclear|whether|if there( i|[’'])s|if any"
    r"|I (think|believe|expect|imagine))\b",
    r"\b(can[’']?t|cannot|can not|couldn[’']?t|could not)\s+(tell|say|confirm|be sure)\b",
    r"^\W*((and|but|so|or|even)\s+)?(if|unless|in case)\b",  # a supposition: "If the warning is a colour, ..."
)
CONTRAST = words(r"^\W*(and\s+)?(but|however|though|yet|although|while|except|only|instead)\b")
GLYPH = re.compile(rf"{BANG}|[\\/(<{{\[]\S{{0,3}}[\\/)>}}\]]")  # a `!`, or a sprite such as /W\ or (@)
SENTENCE = re.compile(r"(?<=\w)!(?=\s|$)|[.?](?=\s|$)|;\s|\n")
PART = re.compile(r"(?<=\w)!(?=\s|$)|[.,:;?](?=\s|$)|\n")  # a clause, by the same `!` rule
STRETCH = re.compile(r":(?=\s|$)")  # what follows a colon explains, out of an earlier denial's reach


def tell(text: str, pellet: bool = False) -> bool:
    """Whether (c) names the tell: a TELL match whose clause, from its start to the match's end, denies nothing
    (and, for ON_INVADER, does not name the shot, unless `pellet`: the screens showed no tell, and the `!` leaving
    the invader is the change they did show)."""
    for pattern in TELL:
        for match in pattern.finditer(text):
            clause = CLAUSE.split(text[: match.start()])[-1] + match.group()
            if not DENIED.search(clause) and (pellet or pattern not in ON_INVADER or not SHOT.search(clause)):
                return True
    return False


def seen(text: str) -> bool:
    """Whether (c) reports what screens with no tell show: see above."""
    if tell(text, pellet=True):
        return True
    for match in AT_INVADER.finditer(text):
        if not DENIED.search(CLAUSE.split(text[: match.start()])[-1] + match.group()):
            return True
    return bool(NO_WARNING.search(text)) and any(
        re.search(BANG, sentence) and SHOT.search(sentence) for sentence in SENTENCE.split(text)
    )


def asserted(text: str) -> bool:
    """Whether (c) asserts as fact a warning the game does not give: see above."""
    for sentence in SENTENCE.split(text):
        if HEDGE.search(sentence):
            continue
        clauses = []
        for stretch in STRETCH.split(sentence):
            denied = False  # a denial reaches the stretch's later clauses, up to one that opens with a CONTRAST
            for clause in PART.split(stretch):
                denied = (denied and not CONTRAST.search(clause)) or bool(DENIED.search(clause))
                if not denied:
                    clauses.append(clause)
        cue = any(
            not (FLASH.fullmatch(match.group()) and GLYPH.search(clause))
            for clause in clauses
            for match in CUE.finditer(clause)
        )
        if cue and any(WARN.search(clause) for clause in clauses):
            return True
    return False


def warning(text: str, tells: bool | None) -> dict:
    """(c) for a run whose screens showed a shooter's tell (tells True), showed none (False), or may have (None: a
    log that cannot say, graded as one that showed a tell, the published rule)."""
    if tells is False:
        return {"pass": seen(text) and not asserted(text), "tells": False}
    return {"pass": tell(text), "tells": tells}


# (d): leave a run: pause, then Q (or the menu).
LEAVE = (words(r"\bpause\w*\b", r"\besc(ape)?\b"), words(r"\bmenu\b"), token("P", "Q"))
RUBRIC = {
    "a": KEYS,
    "b": READOUTS,
    "c": (TELL, DENIED, SHOT, AT_INVADER, NO_WARNING, CUE, WARN, HEDGE, CONTRAST),
    "d": LEAVE,
}


# --- the log --------------------------------------------------------------------------------------------------------


def read_log(path: Path) -> tuple[dict, list[dict], dict | None, str | None]:
    """(header, the command records in order, the exit record, why the run is void)."""
    header, commands, ended, void = {}, [], None, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if "header" in record:
            header = record["header"]
        elif "void" in record:
            void = void or record["void"]
        elif "exit" in record:
            ended = record
        elif isinstance(record.get("n"), int):
            commands.append(record)
    return header, commands, ended, void


def campaign(truth: dict) -> dict | None:
    """The truth's game when it is a Campaign game the player plays (not the demo, not a replay)."""
    game = truth.get("game")
    if not game or truth.get("demo") or truth.get("replay") or game.get("mode") != "classic":
        return None
    return game


def latest_profile(records: list[dict], ended: dict | None, upto: int | None = None) -> dict | None:
    found = None
    for record in records:
        if upto is not None and record["n"] > upto:
            break
        found = record.get("profile", found)
    if upto is None and ended is not None:
        found = ended.get("profile", found)
    return found


def tasks_done(commands: list[dict], ended: dict | None, baseline: bool = False) -> dict[int, int]:
    """{task: the n of the command that first completed it}. baseline: Phase 4's game, which has no initials card
    and no settings (task 7 is dropped and task 5 needs no BOT row). Tasks 3 and 8 also read the scene in front of
    each key, so a pause and resume inside one command (`press p p`) counts, and so does a quit whose last key was
    pressed on the menu (`press q escape escape` from the results)."""
    done: dict[int, int] = {}
    paused, score, scene_before = False, None, None
    for record in commands:
        n, truth = record["n"], record["truth"]
        scene, game = truth.get("scene"), campaign(truth)
        # the scenes the command passed through: the one in front of each key (playtest.py logs it), then its end
        passed = [key["truth"] for key in record["keys"] if "truth" in key] + [truth]
        if 1 not in done and scene == "playing" and game is not None:
            done[1] = n
        if 1 in done and 2 not in done and game is not None and (game.get("kills") or 0) >= 5:
            done[2] = n
        for seen in passed if 1 in done and 3 not in done else ():
            if campaign(seen) is None:
                continue
            if seen.get("scene") == "paused" and seen.get("pause_reason") == "manual":
                paused = True
            elif paused and seen.get("scene") == "playing":
                done[3] = n
                break
        if 1 in done and 4 not in done and game is not None and game.get("phase") in ("game_over", "victory"):
            if scene in ("results", "initials"):
                done[4], score = n, game.get("score")
        if 4 in done and 5 not in done and scene == "menu":
            rows = (latest_profile(commands, None, upto=n) or {}).get("scores") or []
            if baseline or any(row.get("name") == "BOT" and row.get("score") == score for row in rows):
                done[5] = n
        if 5 in done and 6 not in done and scene == "records" and truth.get("shows_run" if baseline else "shows_bot"):
            done[6] = n
        if not baseline and 7 not in done:
            if ((latest_profile(commands, None, upto=n) or {}).get("settings") or {}).get("palette") == "cvd":
                done[7] = n
        quit_from = passed[-2].get("scene") if len(passed) > 1 else scene_before  # the last key's scene, or the
        if 8 not in done and record.get("exited") == 0 and quit_from == "menu":  # last command's in an older log
            done[8] = n
        scene_before = scene
    return done


def tells_shown(commands: list[dict]) -> bool | None:
    """Whether a screen the run showed (each command's end) had a shooter in its tell pose: playtest.py's truth
    "tell". A record from before that field counts as none when it shows no game, or Campaign before wave 5 (pellets
    have no tell, frozen tables), and no demo; any other such record cannot say. True when one showed a tell, else
    None when one cannot say (then (c) is graded as if a tell showed: the published rule), else False."""
    found = False
    for record in commands:
        truth = record["truth"]
        if "tell" in truth:
            shown = bool(truth["tell"])
        else:
            game = truth.get("game")
            wave = game.get("wave") if game else None
            early = game is None or (game.get("mode") == "classic" and isinstance(wave, int) and wave < 5)
            shown = False if early and not truth.get("demo") and truth.get("scene") != "attract" else None
        if shown:
            return True
        if shown is None:
            found = None
    return found


def dead_end(commands: list[dict]) -> tuple[int, str] | None:
    """(the n of the DEAD_END-th command in a row outside play with neither the scene nor the screen changing, that
    scene), the first such."""
    run, previous = 0, None
    for record in commands:
        scene = record["truth"].get("scene")
        key = (scene, record["screen_sha256"])
        run = run + 1 if scene != "playing" and key == previous else 1
        previous = key
        if scene != "playing" and run >= DEAD_END:
            return record["n"], scene or "launch"
    return None


# --- the debrief ----------------------------------------------------------------------------------------------------


def read_debrief(path: Path) -> dict:
    """The debrief's fields a-e: the file's JSON object (or the last one in it)."""
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError:
        data, decoder = None, json.JSONDecoder()
        for start in [m.start() for m in re.finditer(r"\{", text)]:
            try:
                found, _ = decoder.raw_decode(text[start:])
            except ValueError:
                continue
            if isinstance(found, dict) and found.keys() & set("abcde"):
                data = found
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: no JSON object with fields a-e")
    return {key.strip().lower()[:1]: value for key, value in data.items()}


def text_of(value) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}: {text_of(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(text_of(item) for item in value)
    return "" if value is None else str(value)


def grade_debrief(debrief: dict, tells: bool | None = None) -> dict[str, dict]:
    """{question: {"pass": bool, ...what matched}} for a-d; tells: whether the run's screens showed a shooter's tell
    (tells_shown), which (c) is graded against."""
    a, b, c, d = (text_of(debrief.get(q)) for q in "abcd")
    keys = {name: rule(a) for name, rule in KEYS.items()}
    readouts = {name: bool(pattern.search(b)) for name, pattern in READOUTS.items()}
    leave = (bool(LEAVE[0].search(d)) or "P" in LEAVE[2].findall(d)) and (
        bool(LEAVE[1].search(d)) or "Q" in LEAVE[2].findall(d)
    )
    return {
        "a": {"pass": all(keys.values()), "keys": keys},
        "b": {"pass": sum(readouts.values()) >= 3, "readouts": readouts},
        "c": warning(c, tells),
        "d": {"pass": leave},
    }


# --- the run --------------------------------------------------------------------------------------------------------


def grade(log: Path, debrief_path: Path | None, baseline: bool = False) -> dict:
    header, commands, ended, void = read_log(log)
    tasks = [task for task in TASKS if not (baseline and task == 7)]
    done = tasks_done(commands, ended, baseline)
    by_n = {record["n"]: record for record in commands}
    total = len(commands)
    minutes = commands[-1]["end"] / 60.0 if commands else 0.0
    limit = header.get("limit", 300)
    reasons: list[str] = ["void"] if void else []

    def failing(at: int) -> int:
        """The task under way at command `at`: the first not done by then."""
        return next((task for task in tasks if done.get(task) is None or done[task] >= at), tasks[-1])

    finish = done.get(8)
    over = [record["n"] for record in commands if record["n"] > MAX_COMMANDS or record["end"] / 60.0 > MAX_MINUTES]
    if over and (finish is None or finish >= over[0]):
        reasons.append(f"task-{failing(over[0])}:limit")
    stuck = dead_end(commands)
    if stuck is not None and (finish is None or stuck[0] < finish):
        reasons.append(f"task-{failing(stuck[0])}:stuck:{stuck[1]}")
    missing = [task for task in tasks if task not in done]
    if missing and not any(reason.startswith(f"task-{missing[0]}:") for reason in reasons):
        if total >= limit or over:
            reasons.append(f"task-{missing[0]}:limit")
        else:
            scene = commands[-1]["truth"].get("scene") if commands else None
            reasons.append(f"task-{missing[0]}:stuck:{scene or 'launch'}")
    for task in NAMED_TASKS:
        if task in done and not all(key["named"] for key in by_n[done[task]]["keys"]):
            reasons.append(f"task-{task}:unnamed-key")
    guesses = [
        {"n": record["n"], "command": record["command"], "key": key["key"]}
        for record in commands
        for key in record["keys"]
        if not key["named"]
    ]
    debrief, tells = None, tells_shown(commands)
    if debrief_path is not None and debrief_path.exists():
        found = read_debrief(debrief_path)
        debrief = grade_debrief(found, tells)
        debrief["e"] = {"confusing": text_of(found.get("e"))}
        reasons += [f"debrief-{q}" for q in "abcd" if not debrief[q]["pass"]]
    else:
        reasons += [f"debrief-{q}" for q in "abcd"]
    return {
        "pass": not reasons,
        "reasons": reasons,
        "tasks": {task: done.get(task) for task in tasks},
        "commands": total,
        "minutes": round(minutes, 2),
        "dead_end": stuck,
        "tells": tells,
        "guesses": guesses,
        "debrief": debrief,
        "fixture": header.get("fixture"),
        "seed": header.get("seed"),
        "exit": None if ended is None else ended.get("exit"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("debrief", type=Path, nargs="?")
    parser.add_argument("--json", action="store_true", help="print the whole grade as JSON")
    parser.add_argument("--baseline", action="store_true", help="Phase 4's game: no task 7, no initials")
    args = parser.parse_args(argv)
    result = grade(args.log, args.debrief, args.baseline)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        tasks = "  ".join(f"{task}:{'-' if n is None else n}" for task, n in result["tasks"].items())
        print(f"{args.log}: {result['fixture']} seed {result['seed']}, {result['commands']} commands,"
              f" {result['minutes']} virtual minutes, exit {result['exit']}")  # fmt: skip
        print(f"tasks (command that first did each): {tasks}")
        if result["debrief"]:
            print("debrief: " + "  ".join(f"{q} {'ok' if result['debrief'][q]['pass'] else 'MISSED'}" for q in "abcd"))
        shown = {True: "a shooter's tell was on screen", False: "no screen showed a tell", None: "the log cannot say"}
        print(f"(c) graded for a run where {shown[result['tells']]}")
        print(f"guessed keys: {len(result['guesses'])}")
        print("PASS" if result["pass"] else "FAIL " + " ".join(result["reasons"]))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
