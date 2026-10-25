"""G2 (spec 10): move-and-fire works in every terminal.

Each fixture's keyboard, as simkeys plays it (the fixture's protocol and OS repeat timings, locally and over an
SSH link), goes through the whole input path into a running game: VirtualInput -> Decoder -> Session.handle
(Router, KeyGate and the Controls that make_controls picked for the fixture) -> Session.step -> Game.step.
"""

import random
import unittest
from dataclasses import replace
from types import SimpleNamespace

import pytest

from terminal_invaders import app
from terminal_invaders.campaign import held_interval
from terminal_invaders.campaign.testing import staged
from terminal_invaders.input import IntentDriver, VirtualInput
from terminal_invaders.keymap import Keymap
from terminal_invaders.model import TICK_RATE, Config
from terminal_invaders.term.decode import Decoder
from terminal_invaders.term.keys import RELEASE, Key
from terminal_invaders.term.simkeys import PROFILES, Link, RepeatProfile, encode
from tests.support.frames import session
from tests.support.terminals import capabilities, load, names

TICK = 1 / TICK_RATE
INTERVAL = held_interval()  # ticks between volleys with Space held and nothing hit: the two slots' bound (16.5)
LINKS = {"local": None, "ssh": Link(slot=0.02, jitter=0.03)}
pytestmark = pytest.mark.gate  # G2.1


class Rig:
    """A game on a simulated keyboard. run() advances the wall clock tick by tick: the bytes that arrived are
    decoded and routed, then the session steps (when it is ticking), as app.loop does."""

    def __init__(self, fixture, link=None, *, profile=None, controls="auto", seed=7, **settings):
        doc = load(fixture)
        caps = capabilities(doc)
        if link:
            caps = replace(caps, remote=True)  # an ssh session sets SSH_CONNECTION, which the probe reads
        self.session = session(controls=controls, keymap=Keymap.preset("default"), **settings)
        self.decoder = Decoder(esc_timeout=caps.esc_timeout, string_timeout=caps.string_timeout)
        app.configure(self.session, self.decoder, caps)
        self.kitty, self.remote = bool(caps.kitty_flags), caps.remote  # remote: an SSH fixture or the link
        profile = profile or RepeatProfile(fixture, **doc["keys"]["os_repeat"])
        rng = random.Random(seed) if link else None
        self.vin = VirtualInput("kitty" if self.kitty else "legacy", profile, ssh=link, rng=rng)
        self.tick, self.log, self.arrivals, self.sampled = 0, [], [], ()
        sample = self.session.controls.sample

        def recorded(now):
            self.sampled = sample(now)
            return self.sampled

        self.session.controls.sample = recorded

    @property
    def game(self):
        return self.session.pilot.game

    @property
    def scheme(self) -> str:
        return self.session.controls.scheme

    def hold(self, name, down, up):
        self.vin.press(name, down)
        self.vin.release(name, up)

    def run(self, until: float, intend=None):
        """intend(tick): called before each tick's input, as a bot would."""
        while (self.tick + 1) * TICK <= until + 1e-9:
            self.tick += 1
            t = self.tick * TICK
            if intend:
                intend(self.tick)
            for arrival, data in self.vin.bytes_until(t):
                self.decoder.expire(arrival)
                self.decoder.feed(data, arrival)
                self.arrivals.append((arrival, data))
                self.dispatch()
            self.decoder.expire(t)
            self.dispatch()
            ticking = self.session.ticking
            if ticking:
                self.session.step(t)
            events = tuple(self.game.events) if ticking else ()
            self.log.append(SimpleNamespace(t=t, actions=self.sampled if ticking else None, events=events))
            self.log[-1].scene = self.session.scene

    def dispatch(self):
        for event in self.decoder.events():
            assert isinstance(event, Key), event  # neither a downgrade nor a stray reply
            self.session.handle(event, event.time)

    def between(self, start, end):
        return [entry for entry in self.log if start < entry.t <= end]

    def shots(self, start, end):
        return sum("fire" in entry.events for entry in self.between(start, end))


def matrix():
    for fixture in names():
        for where, link in LINKS.items():
            yield fixture, where, link


class MoveAndFireTests(unittest.TestCase):
    def test_hold_right_then_fire_while_still_holding_it(self):
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link)
                rig.hold("d", 0.5, 5.5)
                rig.hold("space", 3.5, 5.5)
                rig.run(5.5)
                moving = rig.between(0.5, 5.5)
                share = sum("right" in entry.actions for entry in moving) / len(moving)
                self.assertGreaterEqual(share, 0.95 if link is None else 0.90)
                firing = rig.between(3.5, 5.5)
                expected = len(firing) / INTERVAL
                if not rig.kitty:
                    # Spec deviation: nothing arrives between the press and the OS's first repeat, and a tap must
                    # fire once, so only the press fires then. 90% of ticks / interval holds up to ~0.55 s delays.
                    delay = rig.vin.profile.delay + (0.05 if link else 0.0)
                    expected = 1 + (len(firing) - delay * TICK_RATE) / INTERVAL
                self.assertGreaterEqual(rig.shots(3.5, 5.5), int(0.9 * expected), (rig.scheme, expected))

    def test_hold_space_then_steer_while_still_holding_it(self):
        # the other order: a tap takes Space's OS repeats, but Space is still down and keeps firing
        for fixture, where, link in matrix():
            for controls in ("auto", "timed"):
                with self.subTest(fixture=fixture, link=where, controls=controls):
                    rig = Rig(fixture, link, controls=controls)
                    rig.hold("space", 2.0, 7.0)
                    if rig.scheme == "hold":
                        rig.hold("d", 3.0, 4.0)
                        rig.hold("a", 5.0, 6.0)
                    else:
                        for name, at in (("d", 3.0), ("s", 4.0), ("a", 5.0), ("s", 6.0)):
                            rig.vin.tap(name, at)
                    rig.run(7.0)
                    self.assertGreaterEqual(rig.shots(3.0, 7.0), int(0.9 * 4.0 * TICK_RATE / INTERVAL), rig.scheme)
                    self.assertTrue(any("left" in e.actions for e in rig.between(5.0, 6.0)))

    def test_a_steer_anywhere_between_two_of_spaces_repeats_keeps_it_firing_and_after_letting_go_does_not(self):
        # Without releases a held Space is read from its OS repeats: the steer that takes them over may land at any
        # point between two of them. Let go of Space and steer past the held-fire gap after its last repeat (0.22 s
        # locally, past 0.12 s; 0.32 s over SSH, an SSH fixture or the link, past 0.25 s even with the link's
        # 50 ms) and fire ends with that repeat's buffer.
        for fixture, where, link in matrix():
            for controls in ("auto", "timed"):
                for phase in (0.1, 0.5, 0.9):
                    with self.subTest(fixture=fixture, link=where, controls=controls, phase=phase):
                        rig = Rig(fixture, link, controls=controls)
                        delay, interval = rig.vin.profile.delay, rig.vin.profile.interval
                        steer = 2.0 + delay + (3 + phase) * interval  # between Space's 4th and 5th OS repeats
                        rig.hold("space", 2.0, steer + 2.0)
                        rig.vin.tap("d", steer)
                        rig.run(steer + 2.0)
                        self.assertGreaterEqual(rig.shots(steer, steer + 2.0), int(0.9 * 2.0 * TICK_RATE / INTERVAL))
                        last = steer + 3.0 + delay + 3 * interval  # Space again; let go after its 4th repeat
                        rig.hold("space", steer + 3.0, last + interval / 2)
                        rig.vin.tap("a", last + (0.32 if rig.remote else 0.22))
                        rig.run(last + 2.0)
                        self.assertEqual(rig.shots(last + 0.35, last + 2.0), 0, rig.scheme)

    def test_quick_taps_of_space_then_a_steer_fire_only_the_taps_own_shots_except_tapped_over_ssh(self):
        # 4 taps at 6 a second, then a steer 0.1 s after the last, are no held Space locally: nothing fires once the
        # last tap's 15-tick buffer has run out (by 0.35 s after it). At 6 a second one tap can wait out its buffer
        # with both shots in flight, with key releases too. Over SSH (an SSH fixture or the link) the tap schemes
        # read the same taps as a held Space, as in Phase 1: they come 1/6 s apart, 0.12-0.22 s through the link,
        # inside the 0.25 s gap kept there so that no real hold is missed, and fire stays on until Space comes
        # again. The hold scheme sees Space's releases, over SSH too.
        for fixture, where, link in matrix():
            for controls in ("auto", "timed"):
                with self.subTest(fixture=fixture, link=where, controls=controls):
                    rig = Rig(fixture, link, controls=controls)
                    taps = [2.0 + n / 6 for n in range(4)]
                    for at in taps:
                        rig.vin.tap("space", at)
                    rig.vin.tap("d", taps[-1] + 0.1)
                    rig.run(5.0)
                    after = rig.shots(taps[-1] + 0.35, 5.0)
                    if rig.remote and rig.scheme != "hold":
                        expected = (5.0 - taps[-1] - 0.35) * TICK_RATE / INTERVAL
                        self.assertGreaterEqual(after, int(0.9 * expected), rig.scheme)  # held fire
                        continue
                    self.assertEqual(after, 0, rig.scheme)
                    self.assertIn(rig.shots(0.0, 5.0), (3, 4), rig.scheme)

    def test_space_that_started_the_run_fires_once_pressed_again(self):
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                taps = Rig(fixture, link, quickstart=False)
                taps.vin.tap("space", 0.5)  # LAUNCH
                for n in range(16):
                    taps.vin.tap("space", 0.75 + 0.25 * n)
                taps.run(5.0)
                self.assertEqual(self.scenes(taps)[:1], ["playing"])
                self.assertGreaterEqual(taps.shots(2.0, 5.0), 8)  # from the wave's start (2 s) to the last tap
                held = Rig(fixture, link, quickstart=False)
                held.vin.tap("space", 0.5)
                held.hold("space", 1.0, 5.0)  # mashed or held: it fires once its repeats say it is down
                held.run(5.0)
                self.assertGreaterEqual(held.shots(2.0, 5.0), int(0.9 * 3.0 * TICK_RATE / INTERVAL))
                duel = Rig(fixture, link, two_player=True)
                duel.vin.tap("space", 0.5)  # the handoff card: Space starts the turn
                for n in range(1, 13):
                    duel.vin.tap("space", 0.5 + 0.4 * n)
                duel.run(5.5)
                self.assertGreaterEqual(duel.shots(2.5, 5.5), 7)

    def test_letting_go_or_tapping_stop_stops_the_ship(self):
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link)
                if rig.scheme == "hold":
                    rig.hold("d", 2.0, 3.0)
                    rig.run(3.5)
                    up = encode(Key("d", kind=RELEASE), kitty=True)
                    (arrived,) = [arrival for arrival, data in rig.arrivals if data == up]
                    stopped = next(e.t for e in rig.log if e.t > arrived and "right" not in e.actions)
                    self.assertLessEqual(stopped - arrived, TICK + 1e-9)
                    continue
                for stop, at in (("s", 3.0), ("a", 5.0)):  # Down/S, or a tap the other way
                    rig.vin.tap("d", at - 1.0)
                    rig.vin.tap(stop, at)
                    rig.run(at + 0.9)
                    self.assertTrue(all("right" in e.actions for e in rig.between(at - 0.95, at)), stop)
                    stopped = next(e.t for e in rig.log if e.t > at and "right" not in e.actions)
                    self.assertLessEqual(stopped - at, (2 if link is None else 4) * TICK + 1e-9, stop)
                    self.assertFalse(any({"left", "right"} & set(e.actions) for e in rig.between(stopped, at + 0.9)))

    def test_four_taps_of_space_are_four_shots(self):
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link)
                for n in range(4):
                    rig.vin.tap("space", 2.0 + 0.25 * n)
                rig.run(3.5)
                self.assertEqual(rig.shots(0.0, 3.5), 4)

    def test_a_space_held_through_the_card_never_takes_it_before_arming(self):
        # Spec 10.2 and 16.2: firing as the wave ends and holding on into the card takes nothing; a fresh press does
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link)
                game = staged(Config("normal", "classic", 7), 3, phase="playing")
                game.aliens.clear()  # the wave's last enemy just went, and no flight is due: next tick, the clear
                game._pending.clear()
                rig.session.pilots[0].game = game
                rig.session.match_controls()
                rig.hold("space", 0.5, 6.0)  # fire held from play, through the clear and the card's arming
                rig.run(6.5)
                self.assertEqual((game.phase, game.picks), ("upgrade", ()), rig.scheme)
                rig.vin.tap("space", 7.0)
                rig.run(7.5)
                self.assertEqual(len(game.picks), 1, rig.scheme)
                self.assertEqual(game.phase, "wave")

    def card_steps(self, link, keys, until=3.5):
        """Cursor moves on a staged card (latched controls, a legacy macOS keyboard) for `keys(rig)`, tick by tick."""
        rig = Rig("apple-terminal-26", link, controls="latched", profile=PROFILES["macos-default"])
        game = staged(Config("normal", "classic", 1978), 4, phase="upgrade")
        rig.session.pilots[0].game = game
        rig.session.match_controls()
        keys(rig)
        cursor, steps = game.cursor, 0
        for tick in range(1, int(until * TICK_RATE) + 1):
            rig.run(tick * TICK)
            self.assertEqual(game.phase, "upgrade")
            steps += game.cursor != cursor
            cursor = game.cursor
        return steps

    def test_every_quick_tap_on_the_card_moves_the_cursor(self):
        # 10.2 and 16.2: each tap is one edge, even the third and fourth of the same way 0.10 s apart (review SI5)
        for where, link in LINKS.items():
            for taps in (2, 3, 4):
                with self.subTest(link=where, taps=taps):
                    steps = self.card_steps(
                        link, lambda rig: [rig.vin.tap("d", 1.0 + 0.10 * n, 0.05) for n in range(taps)]
                    )
                    self.assertEqual(steps, taps)
        for where, link in LINKS.items():  # a held D still moves it no more than its press and repeats did
            with self.subTest(link=where, held=True):
                self.assertLessEqual(self.card_steps(link, lambda rig: rig.hold("d", 1.0, 3.0)), 2)

    def test_holding_b_drops_one_bomb_whatever_the_os_repeat(self):
        for fixture, where, link in matrix():
            for profile in PROFILES.values():  # every delay <= 0.66 s
                with self.subTest(fixture=fixture, link=where, profile=profile.name):
                    rig = Rig(fixture, link, profile=profile)
                    rig.game.player.bombs = 3
                    rig.hold("b", 2.0, 4.0)
                    rig.run(4.5)
                    self.assertEqual(sum("bomb" in e.events for e in rig.log), 1)

    def test_an_os_delay_longer_than_the_guard_is_learned_and_then_one_shot_keys_count_once(self):
        slow = RepeatProfile("accessibility", delay=1.02, interval=0.09)  # past the 0.70 s (1.00 s ssh) guard
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link, profile=slow)
                rig.game.player.bombs = 3
                rig.hold("d", 1.0, 3.0)  # one hold teaches the model the delay
                rig.run(3.5)
                if not rig.kitty:
                    self.assertAlmostEqual(rig.session.router.gate.repeat.delay, 1.02, delta=0.05)
                rig.hold("b", 4.0, 6.5)
                rig.run(7.0)
                self.assertEqual(sum("bomb" in e.events for e in rig.log), 1)
                rig.hold("p", 8.0, 10.5)
                rig.run(11.0)
                self.assertEqual(self.scenes(rig), ["paused"])

    def test_holding_p_toggles_the_pause_once(self):
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link)
                rig.hold("p", 1.0, 3.0)
                rig.run(3.5)
                scenes = [entry.scene for entry in rig.log]
                changes = [b for a, b in zip(scenes, scenes[1:]) if a != b]
                self.assertEqual(changes, ["paused"])

    def test_azerty_zqsd_and_fifty_qs_never_leave_play(self):
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link)
                hold = rig.scheme == "hold"
                rig.vin.tap("z", 2.0)
                rig.hold("q", 2.5, 2.9) if hold else rig.vin.tap("q", 2.5)
                rig.vin.tap("s", 3.0)
                rig.hold("d", 3.5, 3.9) if hold else rig.vin.tap("d", 3.5)
                rig.run(4.0)
                self.assertEqual(rig.shots(2.0, 2.3), 1)
                self.assertTrue(all(set(e.actions) == {"left"} for e in rig.between(2.6, 2.85)))
                self.assertTrue(all(set(e.actions) == set() for e in rig.between(3.1, 3.45)))
                self.assertTrue(all(set(e.actions) == {"right"} for e in rig.between(3.6, 3.85)))
                for n in range(50):
                    rig.vin.tap("q", 4.0 + 0.1 * n)
                rig.run(9.5)
                self.assertEqual({entry.scene for entry in rig.log}, {"playing"})
                self.assertFalse(rig.session.quit)

    def scenes(self, rig):
        scenes = [entry.scene for entry in rig.log]
        return [b for a, b in zip(scenes, scenes[1:]) if a != b]

    def test_space_held_through_game_over_does_not_fire_in_the_next_run(self):
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link)
                rig.hold("space", 2.0, 7.0)  # firing through game over, the results and the next run's intro
                rig.run(3.0)
                rig.game.phase = "game_over"  # as if the last life went
                rig.vin.tap("enter", 4.0)  # again
                rig.run(7.0)
                self.assertEqual(self.scenes(rig), ["results", "playing"])
                self.assertEqual(rig.game.phase, "playing")  # the wave intro is over, and still no shot
                self.assertEqual(rig.shots(4.0, 7.0), 0)
                rig.vin.tap("space", 8.0)  # a fresh press, after the guard
                rig.run(8.5)
                self.assertEqual(rig.shots(8.0, 8.5), 1)

    def test_space_held_into_a_duel_handoff_does_not_start_the_next_pilot(self):
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link, two_player=True)
                rig.vin.tap("enter", 0.5)  # pilot 1 starts
                rig.hold("space", 2.5, 6.0)
                rig.run(3.5)
                rig.game.phase = "game_over"  # pilot 1 is out: the handoff, where Space means start
                rig.run(7.0)
                self.assertEqual(self.scenes(rig), ["playing", "handoff"])
                rig.vin.tap("space", 7.5)
                rig.run(8.0)
                self.assertEqual(self.scenes(rig), ["playing", "handoff", "playing"])
                self.assertEqual(rig.session.active, 1)

    def test_escape_held_on_the_results_goes_to_the_menu_and_no_further(self):
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link)
                rig.run(1.0)
                rig.game.phase = "game_over"
                rig.hold("escape", 2.0, 4.0)  # menu, where Escape means back, then quit
                rig.run(5.0)
                self.assertEqual(self.scenes(rig), ["results", "menu"])
                self.assertEqual((rig.session.selection, rig.session.quit), (0, False))

    def drive(self, rig, plan, until):
        """Play rig with an IntentDriver following plan [(from, held)]: the share of settled ticks (0.4 s after each
        change) where the ship moved as the bot meant."""
        driver = IntentDriver(rig.scheme, rig.vin)
        wanted = {}

        def intend(tick):
            wanted[tick] = next((held for at, held in reversed(plan) if tick * TICK >= at), set())
            driver.intend(tick, frozenset(wanted[tick]))

        rig.run(until, intend)
        settled = [entry for entry in rig.log if all(not 0 <= entry.t - at < 0.4 for at, _ in plan)]
        agree = [{"left", "right"} & set(e.actions) == wanted[round(e.t * TICK_RATE)] - {"fire"} for e in settled]
        return sum(agree) / len(agree)

    def test_a_bot_keyed_by_the_intent_driver_goes_where_it_means_to(self):
        plan = [(0.5, {"right"}), (2.0, set()), (3.0, {"left"}), (4.5, {"right", "fire"}), (6.0, {"fire"})]
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link)
                self.assertGreaterEqual(self.drive(rig, plan + [(7.0, set())], 7.5), 0.95, rig.scheme)
                self.assertGreaterEqual(rig.shots(4.5, 7.0), 8)  # each tap while firing costs an OS delay (legacy)

    def test_reversals_still_work_once_a_short_os_delay_is_learned(self):
        # macos-fast repeats after 0.225 s: a re-tap 120 ms after the first falls within the delay's margin
        plan = [(0.5, {"fire"}), (1.5, {"fire", "right"}), (3.0, {"fire", "left"}), (4.5, {"fire", "right"})]
        plan += [(6.0, {"fire", "left"}), (7.5, set())]
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link, profile=PROFILES["macos-fast"])
                self.assertGreaterEqual(self.drive(rig, plan, 8.0), 0.95, rig.scheme)
                if rig.scheme != "latched":
                    continue
                self.assertIsNotNone(rig.session.router.gate.repeat.delay)
                rig.vin.tap("d", 9.0)  # moving right, then hold the other way: it stops, then reverses
                rig.hold("a", 9.5, 11.0)
                rig.run(11.0)
                self.assertTrue(all("left" in e.actions for e in rig.between(10.0, 11.0)))

    def test_timed_controls_follow_a_bot_that_holds_and_lets_go(self):
        # not while firing: a fire key takes the OS repeat, so a legacy terminal never says the move key let go
        plan = [(0.5, {"right"}), (2.0, set()), (3.0, {"left"}), (4.0, {"right"}), (5.0, set())]
        for fixture, where, link in matrix():
            with self.subTest(fixture=fixture, link=where):
                rig = Rig(fixture, link, controls="timed")
                self.assertGreaterEqual(self.drive(rig, plan, 6.0), 0.95)


class ArcadeFireTests(unittest.TestCase):
    """Arcade fire (spec decision 15) through the whole input path: every tap is one fire input, however fast it
    comes, and a held Space is one, on terminals with and without release events and over SSH."""

    FIXTURES = ("apple-terminal-15", "xterm", "kitty")

    def presses(self, rig, start, end):
        """The fire inputs the game saw: ticks with fire that follow a tick without."""
        sampled = [entry.actions for entry in rig.log if start < entry.t <= end and entry.actions is not None]
        return sum("fire" in now and "fire" not in before for before, now in zip([()] + sampled, sampled))

    def rigs(self, controls=("auto",), **settings):
        """(subtest label, Arcade game rig) for each fixture, link and scheme."""
        for fixture in self.FIXTURES:
            for where, link in LINKS.items():
                for scheme in controls:
                    rig = Rig(fixture, link, mode="arcade", controls=scheme, **settings)
                    yield {"fixture": fixture, "link": where, "scheme": rig.scheme}, rig

    def learn(self, rig):
        """A held D teaches a legacy keyboard's repeat delay; S stops the ship again."""
        rig.hold("d", 1.0, 3.0)
        rig.vin.tap("s", 3.2)
        rig.run(4.0)

    def test_mashed_space_fires_on_every_tap(self):
        for gap in (0.1, 0.125):
            for label, rig in self.rigs(("auto", "timed")):
                with self.subTest(gap=gap, **label):
                    for n in range(20):
                        rig.vin.tap("space", 3.0 + gap * n, 0.04)
                    rig.run(3.0 + gap * 20 + 0.5)
                    self.assertEqual(self.presses(rig, 2.9, rig.tick * TICK), 20)

    def test_taps_at_the_learned_os_delay_all_fire(self):
        for label, rig in self.rigs(profile=PROFILES["macos-default"]):
            with self.subTest(**label):
                self.learn(rig)
                for n in range(10):
                    rig.vin.tap("space", 5.0 + 0.5 * n, 0.05)
                rig.run(10.5)
                self.assertEqual(self.presses(rig, 4.9, 10.5), 10)
                if not rig.kitty:  # a tap there may be the first repeat: it fires once no repeat follows
                    self.assertAlmostEqual(rig.session.router.gate.repeat.delay, 0.525, delta=0.05)

    def test_a_held_space_fires_once(self):
        for profile in (PROFILES["macos-default"], PROFILES["xorg"], RepeatProfile("accessibility", 1.02, 0.09)):
            for label, rig in self.rigs(profile=profile):
                with self.subTest(profile=profile.name, **label):
                    self.learn(rig)
                    rig.hold("space", 5.0, 8.0)
                    rig.run(9.0)
                    self.assertEqual(self.presses(rig, 4.9, 9.0), 1)

    def test_a_slow_key_repeat_is_learned_locally_too(self):
        """A repeat every 0.12-0.19 s is too slow for the delay learning; 5 gaps after the delay, even to 4 ms, teach
        it. Then a held Space fires once, and taps at a steady rate still all fire."""
        for profile in (
            RepeatProfile("slow", 0.525, 0.12),
            RepeatProfile("slower", 0.525, 0.18),
            RepeatProfile("long", 1.02, 0.15),
        ):
            for fixture in ("apple-terminal-15", "xterm"):
                with self.subTest(profile=profile.name, fixture=fixture):
                    rig = Rig(fixture, None, mode="arcade", profile=profile)
                    self.learn(rig)
                    rig.hold("space", 5.0, 8.0)
                    for n in range(10):
                        rig.vin.tap("space", 10.0 + 0.5 * n, 0.05)
                    rig.run(15.5)
                    self.assertEqual(self.presses(rig, 4.9, 9.0), 1)
                    self.assertEqual(self.presses(rig, 9.9, 15.5), 10)
                    self.assertIsNone(rig.session.router.gate.repeat.delay)  # the campaign's learning is untouched

    def test_taps_as_even_as_a_person_can_make_them_are_not_taken_for_a_slow_repeat(self):
        """Gaps 10 ms apart after a pause, as a steady player's are at best: every tap fires, nothing is learned."""
        rig = Rig("xterm", None, mode="arcade", profile=PROFILES["macos-default"])
        times = [5.0, 5.6]
        for n in range(14):
            times.append(times[-1] + (0.155, 0.165)[n % 2])
        for when in times:
            rig.vin.tap("space", when, 0.04)
        rig.run(times[-1] + 0.5)
        self.assertEqual(self.presses(rig, 4.9, times[-1] + 0.5), len(times))
        self.assertIsNone(rig.session.router.gate.repeat.slow)

    def test_before_any_delay_is_learned_a_held_spaces_first_repeat_counts_too(self):
        """The case left (README, Arcade mode): without release events, the first hold before the game knows the
        keyboard's delay reads its first repeat as a second press, and with a slow repeat the four after it too."""
        slower = RepeatProfile("slower", 0.525, 0.18)
        for profile, count in [(profile, 2) for profile in PROFILES.values()] + [(slower, 6)]:
            with self.subTest(profile=profile.name):
                rig = Rig("xterm", None, mode="arcade", profile=profile)
                rig.hold("space", 5.0, 8.0)
                rig.run(9.0)
                self.assertEqual(self.presses(rig, 4.9, 9.0), count)
