"""Arcade mode in the Session, the Painter and the Renderer (spec 8): the 1978 rules at their one difficulty, the
menu and help lines, the Arcade block's fit and too-small screen, its results and panels, the replay banner and p in
the stats line. Most run on FakeArcade; the fire press and the last ones run the real ArcadeGame, a recorded game
replayed to its verified results among them."""

import pytest

from terminal_invaders import app, rules
from terminal_invaders.arcade import ArcadeGame
from terminal_invaders.arcade.view import arcade_layout
from terminal_invaders.model import Config
from terminal_invaders.replay import Playback, Recorder
from terminal_invaders.term.cells import CellBuffer
from terminal_invaders.term.keys import Key
from terminal_invaders.term.pacing import AckPacer
from terminal_invaders.ui import HELP, MODES, Fits, Painter
from tests.support.arcade_fake import HEADER, FakeArcade
from tests.support.frames import options, session, theme
from tests.support.terminals import capabilities, load

DARK = theme(("truecolor", "dark"))


@pytest.fixture
def fake(monkeypatch):
    """Sessions build FakeArcade for the arcade mode."""
    monkeypatch.setitem(rules.RULESETS, rules.ARCADE, FakeArcade)


def drawn(current, cols: int, rows: int) -> tuple[str, Painter]:
    buf = CellBuffer(cols, rows)
    painter = Painter(buf, current.options, DARK)
    painter.draw(current)
    return "\n".join(buf.row_text(y) for y in range(rows)), painter


def printed(content: str, painter: Painter, addr: int, message: str) -> bool:
    """Whether the message the program printed at a VRAM address shows as text where spec 6 puts it."""
    index, p = addr - 0x2400, painter.arcade.p
    x0, y0 = index // 32, 248 - 8 * (index % 32)
    row = content.splitlines()[painter.top + (y0 + 4) // (2 * p)]
    return all(row[painter.left + (x0 + 8 * n) // p] == char for n, char in enumerate(message))


def test_the_menu_offers_arcade_after_classic_at_its_one_difficulty(fake):
    assert MODES == ("classic", "arcade", "endless", "daily", "duel")
    assert ("ARCADE", "The 1978 rules: tap to fire, no upgrades.") in HELP
    current = session(quickstart=False, difficulty="hard")
    current.mode = "arcade"
    content = drawn(current, 80, 24)[0]
    assert "DIFFICULTY   <   1978    >" in content and "HARD" not in content
    assert "The 1978 original: 55 invaders, one shot." in content
    current.start()
    game = current.pilot.game
    assert type(game) is FakeArcade and game.config == Config("normal", "arcade", 1978)
    assert (current.scene, current.controls.press_fire) == ("playing", True)


def test_the_records_page_shows_the_arcade_board_whatever_difficulty_was_picked(fake):
    current = session(quickstart=False, difficulty="hard")
    row = dict(name="ZED", score=4410, wave=2, mode="arcade", difficulty="normal", seed=1978, ruleset=rules.ARCADE)
    current.profile.data["scores"].append(row)
    current.mode = "arcade"
    current.change_scene("records")
    content = drawn(current, 80, 24)[0]
    assert "FLIGHT RECORDS - ARCADE / 1978" in content and "ZED" in content and "4410" in content
    assert "NOVA" not in content and "HARDY" not in content  # the campaign's boards
    lines = content.splitlines()
    assert lines[3].split() == ["PILOT", "SCORE", "ROUND", "MODE", "DIFFICULTY"]
    assert lines[5].split() == ["ZED", "4410", "2", "arcade", "1978"]  # the 1978 rules, whatever the row stored
    current.mode = "classic"
    assert drawn(current, 80, 24)[0].splitlines()[3].split()[:3] == ["PILOT", "SCORE", "WAVE"]


def test_a_fire_press_reaches_the_game_for_one_tick():
    current = session(mode="arcade")
    assert isinstance(current.pilot.game, ArcadeGame)
    seen, step = [], current.pilot.game.step
    current.pilot.game.step = lambda actions: seen.append("fire" in actions) or step(actions)
    current.handle(Key("space"), 0.0)
    for n in range(1, 30):
        current.step(n / 60)
    assert seen == [True] + [False] * 28


def test_on_resize_takes_both_fits_or_one_bool(fake):
    current = session(mode="arcade")
    current.on_resize(Fits(campaign=True, arcade=False))
    assert (current.scene, current.pause_reason, current.ticking) == ("paused", "small", False)
    current.on_resize(Fits(campaign=False, arcade=True))
    assert (current.pause_reason, current.fits) == ("resized", True)  # the Arcade block is what matters
    current.on_resize(False)
    assert current.pause_reason == "small"
    current.on_resize(True)
    assert current.pause_reason == "resized"
    classic = session()
    classic.on_resize(Fits(campaign=True, arcade=False))
    assert classic.pause_reason == "resized"


def test_an_arcade_game_started_in_a_window_too_small_for_it_pauses(fake):
    current = session(quickstart=False)
    current.room = Fits(campaign=True, arcade=False)
    current.mode = "arcade"
    assert not current.fits  # before the first game: the selected mode's block
    current.start()
    assert (current.scene, current.pause_reason) == ("paused", "small")
    content = drawn(current, 80, 24)[0]
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    assert lines == ["ARCADE NEEDS 56 x 32", "Current: 80 x 24 | Paused", "Enlarge the window or zoom out"]
    current.mode = "classic"
    current.start()
    assert current.scene == "playing"


def test_the_arcade_block_draws_the_1978_screen_at_each_scale(fake):
    current = session(mode="arcade")
    for _ in range(120):
        current.step(0)
    for (cols, rows), p in (((224, 128), 1), ((112, 64), 2), ((75, 43), 3), ((56, 32), 4), ((150, 90), 2)):
        content, painter = drawn(current, cols, rows)
        layout = arcade_layout(cols, rows)
        assert painter.arcade.p == p
        assert (painter.width, painter.left, painter.top) == (layout.width, layout.left, layout.top)
        assert printed(content, painter, HEADER, " SCORE<1> HI-SCORE SCORE<2> ")
        assert any(ch in content for ch in "▀▄")


def test_the_panels_draw_over_the_arcade_block_without_a_seed(fake):
    current = session(mode="arcade")
    current.pause("manual")
    content = drawn(current, 112, 64)[0]
    assert "PAUSED" in content and "SEED" not in content and "S   C   O   R   E" in content
    current.resume(0.0)
    assert "PAUSED" not in drawn(current, 112, 64)[0]  # a manual pause resumes at once
    current.on_resize(True)
    current.resume(0.0)
    assert "GET READY" in drawn(current, 112, 64)[0]


def test_the_results_say_game_over_with_the_round_and_no_seed(fake, monkeypatch):
    monkeypatch.setattr(FakeArcade, "length", 40)
    current = session(mode="arcade")
    while current.scene == "playing":
        current.handle(Key("space"), current.now + 0.5)
        current.step(current.now + 0.5)
    game = current.pilot.game
    assert current.scene == "results" and game.phase == "game_over"
    content = drawn(current, 112, 64)[0]
    accuracy = 100 * game.hits / max(1, game.shots_fired)
    assert "GAME OVER" in content and f"SCORE {game.score:,}    ROUND 1" in content
    assert f"{game.kills} kills   {accuracy:.0f}% accuracy" in content and "SEED" not in content
    assert "UNLOCKED Deep space" in content


def test_a_replayed_arcade_game_shows_its_banner_on_the_blocks_last_row(fake, tmp_path):
    game = FakeArcade()
    recorder = Recorder(game.config)
    for tick in range(30):
        actions = ("fire",) if tick % 7 == 0 else ()
        recorder.append(actions)
        game.step(actions)
    path = tmp_path / "arcade.json"
    recorder.save(path, game)
    current = session(Playback(path))
    current.step(0)
    content, painter = drawn(current, 112, 64)
    assert content.splitlines()[painter.top + painter.height - 1].strip().startswith("REPLAY")


def test_the_renderer_reports_both_fits_and_the_stats_line_shows_p(fake):
    current = session(mode="arcade")
    for size, fits, scale in (((112, 64), Fits(True, True), "p2"), ((80, 24), Fits(True, False), "p4")):
        renderer = app.Renderer(options(mode="arcade"), capabilities(load("ghostty")), size)
        renderer.painter.draw(current)
        assert renderer.fits == fits
        assert renderer.stats(current, AckPacer("off")).endswith(f"latched {scale} truecolor")
    current.mode = "classic"
    current.start()
    assert renderer.stats(current, AckPacer("off")).endswith("latched k1 truecolor")


def test_a_session_plays_the_real_arcade_game():
    current = session(mode="arcade")
    game = current.pilot.game
    assert isinstance(game, ArcadeGame) and current.controls.press_fire
    fired = []
    for n in range(600):
        if n % 20 == 0:
            current.handle(Key("space"), n / 60)
        shots = game.shots_fired
        current.step(n / 60)
        fired += [n] * (game.shots_fired - shots)
    assert game.tick == 600 and game.phase == "playing"
    # Each shot leaves on a press's own frame, once the cannon is in play and its last shot has gone (it was
    # 320, 400, 480 and 560 when this was written).
    assert len(fired) >= 4 and all(n % 20 == 0 for n in fired) and fired == sorted(set(fired))
    content, painter = drawn(current, 112, 64)
    assert printed(content, painter, HEADER, " SCORE<1> HI-SCORE SCORE<2>")
    assert printed(content, painter, game.hi_score_addr, "1250")  # the profile's best over the HI-SCORE field
    assert sum(content.count(ch) for ch in "▀▄") > 100  # the fleet, the shields, the cannon
    assert "ARCADE NEEDS 56 x 32" in drawn(current, 80, 24)[0]


def test_the_real_arcade_game_reaches_its_results():
    current = session(mode="arcade")
    game = current.pilot.game
    while current.scene == "playing" and game.tick < 60 * 60 * 10:
        current.step(0)
    assert current.scene == "results" and game.phase == "game_over"
    content = drawn(current, 112, 64)[0]
    assert "GAME OVER" in content and f"SCORE {game.score:,}    ROUND {game.wave}" in content


def test_a_recorded_real_arcade_game_replays_to_verified_results(tmp_path):
    game = ArcadeGame(Config("normal", "arcade", 1978))
    recorder = Recorder(game.config)
    while game.phase == "playing" and game.tick < 60 * 60 * 10:
        actions = ("fire",) if game.tick % 20 == 0 else ()
        recorder.append(actions)
        game.step(actions)
    assert game.phase == "game_over" and game.shots_fired > 0
    path = tmp_path / "arcade.json"
    recorder.save(path, game)
    current = session(Playback(path))
    while current.scene == "playing" and current.pilot.game.tick <= game.tick:
        current.step(0)
    replayed = current.pilot.game
    assert (current.scene, current.replay_verified) == ("results", True)
    assert (replayed.tick, replayed.digest()) == (game.tick, game.digest())
    content = drawn(current, 112, 64)[0]
    assert "REPLAY VERIFIED" in content and f"SCORE {game.score:,}    ROUND {game.wave}" in content
