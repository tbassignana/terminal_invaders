"""Timing, input, session boundaries, and terminal portability regressions."""

import curses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from terminal_invaders.engine import Game
from terminal_invaders.model import Config, Shot
from terminal_invaders.replay import Playback, Recorder, save_tapes
from terminal_invaders.storage import Profile
from terminal_invaders.ui import Controls, FixedClock, Painter, Session, _loop


def session(**overrides):
    options = dict(
        mode="classic",
        difficulty="normal",
        two_player=False,
        seed=42,
        fixed_seed=True,
        quickstart=True,
        name="TEST",
        record=None,
        mono=True,
        reduced_motion=False,
        show_fps=False,
        fps=60,
    )
    options.update(overrides)
    profile = Mock(data={"scores": [], "stats": {}, "achievements": []}, error=None, alert=None)
    profile.record.return_value = []
    profile.best.return_value = 0
    audio = Mock(enabled=False)
    return Session(SimpleNamespace(**options), profile, audio)


class TimingTests(unittest.TestCase):
    def test_fixed_steps_match_elapsed_time_at_different_render_rates(self):
        for fps in (15, 24, 30, 60, 120, 144):
            clock = FixedClock(0)
            ticks = sum(clock.advance(frame / fps, True) for frame in range(1, fps * 10 + 1))
            self.assertEqual(ticks, 600)

    def test_pause_discards_partial_tick_and_long_wait(self):
        clock = FixedClock(0)
        self.assertEqual(clock.advance(0.01, True), 0)
        self.assertEqual(clock.advance(300, False), 0)
        self.assertEqual(clock.advance(300.01, True), 0)
        self.assertEqual(clock.advance(300.02, True), 0)
        self.assertEqual(clock.advance(300.03, True), 1)

    def test_catchup_is_bounded_without_future_backlog(self):
        clock = FixedClock(0)
        self.assertEqual(clock.advance(100, True), 8)
        self.assertEqual(clock.advance(100.001, True), 0)

    def test_repeat_coalesces_actions_and_opposite_direction_wins(self):
        controls = Controls()
        for _ in range(200):
            controls.press("bomb", 1)
            controls.press("autofire", 1)
        controls.press("left", 1)
        controls.press("right", 1.01)
        controls.press("fire", 1.01)
        self.assertEqual(controls.sample(1.02), ("autofire", "bomb", "fire", "right"))
        self.assertEqual(controls.sample(1.03), ("fire", "right"))
        self.assertEqual(controls.sample(2), ())


class SessionTests(unittest.TestCase):
    def test_pause_freezes_entire_state_and_clears_pending_input(self):
        current = session()
        current.handle(ord("b"), 0)
        current.handle(ord("p"), 0)
        before = current.pilot.game.digest()
        current.step(100)
        self.assertEqual(current.pilot.game.digest(), before)
        self.assertEqual(current.controls.sample(100), ())
        current.handle(27, 100)
        current.step(100)
        self.assertEqual(current.pilot.game.tick, 1)

    def test_duel_uses_equal_independent_games_and_changes_after_hit(self):
        current = session(two_player=True, name="ABCDEFGHIJ")
        first, second = current.pilots
        self.assertEqual(first.game.digest(), second.game.digest())
        self.assertEqual(first.name, "ABCDEFGH-1")
        current.handle(10, 0)
        game = first.game
        game.phase = "playing"
        game.shots = [Shot(game.player.x, game.player.y - 0.1, friendly=False, vy=0.2)]
        current.step(0)
        self.assertEqual(current.active, 1)
        self.assertEqual(current.scene, "handoff")
        self.assertEqual(second.game.tick, 0)
        self.assertEqual(first.game.player.lives, second.game.player.lives - 1)

    def test_completed_game_saves_once_and_moves_to_results(self):
        current = session()
        current.pilot.game.phase = "game_over"
        current.step(0)
        current.step(1)
        self.assertEqual(current.scene, "results")
        current.profile.record.assert_called_once()

    def test_menu_keyboard_and_display_shortcuts(self):
        current = session(quickstart=False)
        current.handle(ord("S"), 0)
        current.handle(curses.KEY_RIGHT, 0)
        self.assertEqual(current.mode, "endless")
        current.handle(ord("w"), 0)
        current.handle(ord(" "), 0)
        self.assertEqual(current.scene, "playing")
        current.handle(curses.KEY_F1, 0)
        self.assertTrue(current.options.show_fps)

    def test_daily_ignores_selected_difficulty(self):
        current = session(mode="daily", difficulty="hard")
        self.assertEqual(current.pilot.game.config.difficulty, "normal")

    def test_replay_ignores_gameplay_keys_and_never_records_score(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flight.json"
            original = Game(Config(seed=9))
            recorder = Recorder(original.config)
            for _ in range(7):
                recorder.append(("left",))
                original.step(("left",))
            recorder.save(path, original)
            current = session()
            current.replay = Playback(path)
            current.start()
            current.handle(ord("r"), 0)
            current.handle(ord("b"), 0)
            current.handle(ord("f"), 0)
            for _ in range(7):
                current.step(0)
            self.assertTrue(current.replay_verified)
            self.assertEqual(current.scene, "results")
            self.assertEqual(current.pilot.game.digest(), original.digest())
            current.profile.record.assert_not_called()

    def test_quit_to_menu_saves_a_verifiable_partial_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flight.json"
            current = session(record=path)
            current.step(0)
            current.handle(ord("q"), 0)
            (replay,) = Playback(path).tapes
            self.assertTrue(replay.verify(current.pilot.game))
            self.assertEqual(current.scene, "menu")

    def test_restart_keeps_replay_failure_visible(self):
        current = session(record=Path("unused.json"))
        with patch("terminal_invaders.ui.save_tapes", side_effect=OSError("disk unavailable")):
            current.start()
        self.assertIn("disk unavailable", current.note)

    def test_duel_records_every_pilot_into_one_verifiable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duel.json"
            current = session(two_player=True, record=path)
            for active, ticks in ((0, 5), (1, 3), (0, 2)):
                current.active = active
                current.change_scene("playing")
                for _ in range(ticks):
                    current.step(0)
                current.save_replay()
            current.handle(ord("q"), 0)
            tapes = Playback(path).tapes
            self.assertEqual([(tape.pilot, tape.ticks) for tape in tapes], [(1, 7), (2, 3)])
            self.assertTrue(all(tape.verify(tape.simulate()) for tape in tapes))

    @patch("terminal_invaders.ui.curses.doupdate")
    def test_replay_watches_every_tape_in_order(self, update):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duel.json"
            tapes = []
            for pilot, ticks in ((1, 4), (2, 6)):
                game, recorder = Game(Config(seed=9)), Recorder(Config(seed=9), pilot)
                for _ in range(ticks):
                    recorder.append(("right",))
                    game.step(("right",))
                tapes.append(recorder.tape(game))
            save_tapes(path, tapes)
            current = session()
            current.replay = Playback(path)
            current.start()
            for _ in range(4):
                current.step(0)
            self.assertEqual((current.active, current.scene), (1, "playing"))
            for _ in range(6):
                current.step(0)
            self.assertEqual(current.scene, "results")
            self.assertTrue(current.replay_verified)
            self.assertEqual([(pilot.game.tick, pilot.verified) for pilot in current.pilots], [(4, True), (6, True)])
            screen = FakeScreen()
            Painter(screen, current.options).draw(current, 60)
            self.assertIn("REPLAY VERIFIED", screen.content())
            self.assertIn("P2 0  WAVE 1  OK", screen.content())


class FakeScreen:
    def __init__(self, rows=24, columns=60):
        self.rows, self.columns = rows, columns
        self.cells = {}

    def getmaxyx(self):
        return self.rows, self.columns

    def addstr(self, y, x, text, attr):
        assert 0 <= y < self.rows and 0 <= x < self.columns
        assert x + len(text) <= self.columns
        for index, char in enumerate(text):
            self.cells[y, x + index] = char

    def erase(self):
        self.cells.clear()

    def noutrefresh(self):
        pass

    def content(self):
        return "\n".join("".join(self.cells.get((y, x), " ") for x in range(self.columns)) for y in range(self.rows))


class DrawingTests(unittest.TestCase):
    @patch("terminal_invaders.ui.curses.curs_set")
    @patch("terminal_invaders.ui.curses.set_escdelay")
    def test_idle_screens_wait_without_redrawing_until_resize(self, *mocks):
        for scene in ("menu", "help", "records", "paused", "handoff", "results", "too_small"):
            with self.subTest(scene=scene):
                current = session()
                current.scene = "playing" if scene == "too_small" else scene
                screen = FakeScreen(columns=30 if scene == "too_small" else 60)
                screen.keypad, screen.timeout, screen.nodelay = Mock(), Mock(), Mock()
                elapsed, polls, pending = 0.0, 0, False

                def getch():
                    nonlocal elapsed, polls, pending
                    if pending:
                        pending = False
                        return -1
                    elapsed += 0.1
                    polls += 1
                    if polls == 10:
                        current.quit = True
                    if polls == 5:
                        pending = True
                        return curses.KEY_RESIZE
                    return -1

                screen.getch = getch
                with patch("terminal_invaders.ui.time.monotonic", side_effect=lambda: elapsed):
                    with patch.object(Painter, "draw") as draw:
                        _loop(screen, current)
                self.assertEqual(draw.call_count, 2)  # Initial image, then the resize.
                self.assertEqual(current.pilot.game.tick, 0)
                self.assertTrue(all(call.args == (100,) for call in screen.timeout.call_args_list))

    @patch("terminal_invaders.ui.curses.curs_set")
    @patch("terminal_invaders.ui.curses.set_escdelay")
    @patch("terminal_invaders.ui.curses.doupdate")
    def test_buffered_input_is_bounded_without_dropping_overflow_key(self, *mocks):
        current = session()
        keys = [ord("f")] * 32 + [ord("q"), ord("q")]
        remaining = keys.copy()
        screen = FakeScreen()

        def getch():
            if remaining:
                return remaining.pop(0)
            current.quit = True
            return -1

        screen.getch = getch
        screen.keypad, screen.timeout, screen.nodelay = Mock(), Mock(), Mock()
        with patch.object(current, "handle", wraps=current.handle) as handle:
            _loop(screen, current)
        self.assertEqual([call.args[0] for call in handle.call_args_list], keys)
        self.assertGreaterEqual(screen.timeout.call_count, 2)
        self.assertTrue(all(call.args[0] > 0 for call in screen.timeout.call_args_list))

    @patch("terminal_invaders.ui.curses.doupdate")
    def test_all_screens_fit_minimum_size_and_render_in_monochrome(self, update):
        current = session()
        screen = FakeScreen()
        painter = Painter(screen, current.options)
        for scene in ("menu", "help", "records", "playing", "paused", "handoff", "results"):
            current.scene = scene
            painter.draw(current, 60)
            self.assertTrue(screen.cells)

    @patch("terminal_invaders.ui.curses.doupdate")
    def test_tiny_and_resized_windows_are_clipped_and_centered(self, update):
        current = session()
        for rows, columns in ((1, 1), (10, 30), (24, 59), (40, 100)):
            screen = FakeScreen(rows, columns)
            painter = Painter(screen, current.options)
            painter.draw(current, 60)
            self.assertEqual(painter.resize(), rows >= 24 and columns >= 60)

    @patch("terminal_invaders.ui.curses.doupdate")
    def test_records_keep_different_difficulty_boards_separate(self, update):
        current = session()
        current.profile.data["scores"] = [
            dict(name="NORMAL", score=1, wave=1, mode="classic", difficulty="normal", seed=0),
            dict(name="HARD", score=99, wave=1, mode="classic", difficulty="hard", seed=0),
        ]
        current.scene = "records"
        screen = FakeScreen()
        Painter(screen, current.options).draw(current, 60)
        self.assertIn("NORMAL", screen.content())
        self.assertNotIn("HARD", screen.content())

    @patch("terminal_invaders.ui.curses.doupdate")
    def test_profile_alerts_are_shown_whole_on_the_menu_and_results(self, update):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            for content in ('{"version": 3, "name": "FUTURE"}', "{bad json"):
                path.write_text(content)
                current = session()
                current.profile = Profile(path)
                for scene in ("menu", "results"):
                    with self.subTest(content=content, scene=scene):
                        current.scene = scene
                        screen = FakeScreen()
                        Painter(screen, current.options).draw(current, 60)
                        self.assertIn(current.profile.alert, screen.content())

    @patch("terminal_invaders.ui.curses.init_pair", side_effect=curses.error)
    @patch("terminal_invaders.ui.curses.use_default_colors")
    @patch("terminal_invaders.ui.curses.start_color")
    @patch("terminal_invaders.ui.curses.has_colors", return_value=True)
    def test_failed_color_setup_falls_back_to_monochrome(self, *mocks):
        current = session(mono=False)
        painter = Painter(FakeScreen(), current.options)
        self.assertEqual(painter.colors, {})


if __name__ == "__main__":
    unittest.main()
