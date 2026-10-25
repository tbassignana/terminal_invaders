"""Input semantics (spec 6.7, 6.8, 6.11): the Router, the three control schemes, make_controls, and the simulated
keyboards and players that drive them."""

import math
import random
import signal
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from terminal_invaders.input import (
    FIRE_BUFFER_TICKS,
    HOLD_NOTE,
    PHYSICAL_NOTE,
    HoldControls,
    IntentDriver,
    LatchedControls,
    Router,
    TimedControls,
    VirtualInput,
    make_controls,
)
from terminal_invaders.keymap import PRESETS, Keymap
from terminal_invaders.model import TICK_RATE
from terminal_invaders.term.gate import KeyGate
from terminal_invaders.term.keys import CTRL, PRESS, RELEASE, REPEAT, SHIFT, Key
from terminal_invaders.term.repeat import RepeatModel
from terminal_invaders.term.simkeys import PROFILES, Link, encode, stream
from tests.support.frames import session
from tests.support.terminals import capabilities, load

TICK = 1 / TICK_RATE
FAST = PROFILES["macos-fast"]


def press(name, t=0.0, **kwargs):
    return Key(name, kind=PRESS, time=t, **kwargs)


def release(name, t=0.0, **kwargs):
    return Key(name, kind=RELEASE, certain=True, time=t, **kwargs)


def repeat(name, t=0.0):
    return Key(name, kind=REPEAT, certain=True, time=t)


class Spy:
    """Controls that record what the Router hands them."""

    scheme, note, press_fire = "spy", "", False

    def __init__(self):
        self.fed, self.pulses, self.deferred = [], [], []

    def feed(self, action, key, edge, now):
        self.fed.append((action, key.kind, edge))

    def pulse(self, action):
        self.pulses.append(action)

    def defer_fire(self, due):
        self.deferred.append(due)


class RouterTests(unittest.TestCase):
    def rig(self, context="play", *, certain=False, remote=False):
        commands = []
        target = SimpleNamespace(context=context, controls=Spy(), command=lambda name, now: commands.append(name))
        router = Router(Keymap.preset("default"), KeyGate(RepeatModel(remote=remote), certain=certain))
        return router, target, commands

    def test_controls_pulses_and_commands_go_their_own_ways(self):
        router, target, commands = self.rig(certain=True)
        for key in (press("d"), release("d"), press("b"), press("p"), press("m")):
            router.route(target, key, 0.0)
        self.assertEqual(target.controls.fed, [("right", PRESS, True), ("right", RELEASE, False)])
        self.assertEqual(target.controls.pulses, ["bomb"])
        self.assertEqual(commands, ["pause", "sound"])  # play's own command, then a global one

    def test_kitty_signal_keys_raise_their_signal_on_press_only_and_pass_no_gate(self):
        router, target, commands = self.rig(certain=True)
        with patch.object(signal, "raise_signal") as raised, patch.object(router.gate, "admit") as admit:
            for name, number in (("c", signal.SIGINT), ("z", signal.SIGTSTP), ("\\", signal.SIGQUIT)):
                router.route(target, press(name, mods=CTRL), 0.0)
                router.route(target, release(name, mods=CTRL), 0.0)
                raised.assert_called_with(number)
        self.assertEqual(raised.call_count, 3)
        admit.assert_not_called()

    def test_unbound_keys_are_tracked_by_the_gate_but_reach_nothing(self):
        router, target, commands = self.rig("pause", certain=True)
        router.route(target, press("d"), 0.0)  # unbound in the pause menu, and still held when play resumes
        target.context = "play"
        router.gate.scene_changed(0.1)
        router.route(target, repeat("d"), 0.2)
        router.route(target, release("d"), 0.3)
        router.route(target, press("d"), 0.4)
        self.assertEqual(target.controls.fed, [("right", PRESS, True)])  # only the fresh press after the release
        self.assertEqual(commands, [])

    def test_an_unbound_key_ends_another_keys_repeat_run(self):
        router, target, _ = self.rig()
        router.route(target, press("d"), 0.0)
        router.route(target, press("d"), 0.03)  # a bounce: no edge
        router.route(target, press("y"), 0.06)  # unbound in play; the OS now repeats y
        router.route(target, press("d"), 0.09)
        self.assertEqual([edge for _, _, edge in target.controls.fed], [True, False, True])

    def test_over_ssh_every_fire_press_is_an_edge(self):
        for remote, expected in ((False, [True, True, False]), (True, [True, True, True])):
            with self.subTest(remote=remote):
                router, target, _ = self.rig(remote=remote)
                for t in (0.0, 0.2, 0.3):  # the first repeat, then a fast one, as the model sees them
                    router.route(target, press("space"), t)
                router.route(target, press("d"), 0.5)
                router.route(target, press("d"), 0.52)
                fire = [edge for action, _, edge in target.controls.fed if action == "fire"]
                moves = [edge for action, _, edge in target.controls.fed if action == "right"]
                self.assertEqual(fire, expected)
                self.assertEqual(moves, [True, False])  # moves keep the model's verdict

    def test_a_press_only_fire_takes_the_models_press_verdict_locally_and_over_ssh(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                router, target, _ = self.rig(remote=remote)
                target.controls.press_fire = True  # an Arcade game: every tap fires, a held Space never again
                for t in (0.0, 0.1, 0.2, 0.3, 0.42, 0.75, 0.78):  # mashed, then at an OS delay and repeating
                    router.route(target, press("space"), t)
                fire = [edge for _, _, edge in target.controls.fed]
                self.assertEqual(fire, [True, True, True, True, True, True, False])
                self.assertEqual(target.controls.deferred, [])  # no delay learned yet: nothing waits

    def test_a_press_only_fire_at_the_learned_delay_waits_for_the_repeats_that_would_follow(self):
        router, target, _ = self.rig()
        target.controls.press_fire = True
        for t in (0.0, 0.5, 0.55, 0.6, 0.65):  # a held D teaches the model a 0.5 s delay
            router.route(target, press("d"), t)
        model = router.gate.repeat
        self.assertAlmostEqual(model.delay, 0.5)
        for t in (2.0, 2.5, 3.0):
            router.route(target, press("space"), t)
        self.assertEqual([edge for action, _, edge in target.controls.fed if action == "fire"], [True, None, None])
        self.assertEqual(target.controls.deferred, [2.5 + model.hold_gap, 3.0 + model.hold_gap])


class HoldControlsTests(unittest.TestCase):
    def test_the_ship_moves_while_a_key_is_down_and_the_latest_press_wins(self):
        controls = HoldControls()
        controls.feed("right", press("d"), True, 0.0)
        self.assertEqual(controls.sample(0.0), ("right",))
        controls.feed("left", press("a"), True, 0.1)
        self.assertEqual(controls.sample(0.1), ("left",))
        controls.feed("left", repeat("a"), False, 0.2)
        controls.feed("right", repeat("d"), False, 0.2)  # a repeat of a held key is no new press
        self.assertEqual(controls.sample(0.2), ("left",))
        controls.feed("left", release("a"), False, 0.3)
        self.assertEqual(controls.sample(0.3), ("right",))
        controls.feed("right", release("d"), False, 0.4)
        self.assertEqual(controls.sample(0.4), ())

    def test_a_press_and_release_inside_one_tick_moves_once_and_fires_once(self):
        controls = HoldControls()
        for action, name in (("left", "a"), ("fire", "space")):
            controls.feed(action, press(name), True, 0.0)
            controls.feed(action, release(name), False, 0.005)
        self.assertEqual(controls.sample(0.01), ("fire", "left"))
        controls.fired()
        self.assertEqual(controls.sample(0.02), ())

    def test_a_fire_tap_waits_out_the_cooldown_and_a_held_fire_key_keeps_firing(self):
        controls = HoldControls()
        controls.feed("fire", press("space"), True, 0.0)
        controls.feed("fire", release("space"), False, 0.05)
        self.assertEqual([controls.sample(n * TICK) for n in range(FIRE_BUFFER_TICKS + 1)][-2:], [("fire",), ()])
        controls.feed("fire", press("space"), True, 1.0)
        controls.sample(1.0)
        controls.fired()
        self.assertEqual(controls.sample(1.1), ("fire",))  # still down

    def test_stop_drops_held_moves_and_a_key_held_since_before_tracking_counts_as_pressed(self):
        controls = HoldControls()
        controls.feed("right", press("d"), True, 0.0)
        controls.feed("stop", press("s"), True, 0.1)
        self.assertEqual(controls.sample(0.1), ())
        controls.feed("left", repeat("a"), False, 0.2)  # its press came before a clear()
        self.assertEqual(controls.sample(0.2), ("left",))
        controls.pulse("bomb")
        self.assertEqual(controls.sample(0.3), ("bomb", "left"))
        self.assertEqual(controls.sample(0.4), ("left",))
        controls.clear()
        self.assertEqual(controls.sample(0.5), ())


class LatchedControlsTests(unittest.TestCase):
    def test_a_tap_latches_until_the_opposite_tap_or_a_stop(self):
        controls = LatchedControls()
        script = [
            ("right", True, ("right",)),
            ("right", False, ("right",)),  # an OS repeat
            ("right", True, ("right",)),  # the same way again: no change
            ("left", True, ()),  # the opposite way stops
            ("left", False, ()),  # a repeat does not start it again
            ("left", True, ("left",)),
            ("stop", False, ()),  # any stop key event stops
        ]
        for n, (action, edge, expected) in enumerate(script):
            controls.feed(action, press("x"), edge, n * 0.1)
            self.assertEqual(controls.sample(n * 0.1), expected, (n, action, edge))
        controls.feed("right", release("d"), True, 1.0)  # releases mean nothing to a latch
        self.assertEqual(controls.sample(1.0), ())

    def test_each_fire_event_arms_the_buffer_and_a_shot_disarms_it(self):
        controls = LatchedControls()
        controls.feed("fire", press("space"), True, 0.0)
        fired = [bool(controls.sample(n * TICK)) for n in range(FIRE_BUFFER_TICKS + 2)]
        self.assertEqual(fired, [True] * FIRE_BUFFER_TICKS + [False, False])
        controls.feed("fire", press("space"), False, 1.0)  # an OS repeat re-arms it
        self.assertEqual(controls.sample(1.0), ("fire",))
        controls.fired()
        self.assertEqual(controls.sample(1.01), ())
        controls.fire_repeats = False
        controls.feed("fire", press("space"), False, 1.1)
        self.assertEqual(controls.sample(1.1), ())

    def test_pulses_go_out_once_and_clear_forgets_everything(self):
        controls = LatchedControls("note")
        controls.feed("left", press("a"), True, 0.0)
        controls.pulse("autofire")
        self.assertEqual(controls.sample(0.0), ("autofire", "left"))
        self.assertEqual(controls.sample(0.1), ("left",))
        controls.clear()
        self.assertEqual((controls.sample(0.2), controls.note), ((), "note"))


class HeldFireTests(unittest.TestCase):
    """Legacy fire (both tap schemes): another key takes a held Space's OS repeats, but not the hold."""

    REPEATING = [0.0, 0.5, 0.59, 0.68, 0.77]  # press, the OS delay, repeats

    def fired_ticks(self, controls, start, end):
        return sum("fire" in controls.sample(n * TICK) for n in range(round(start / TICK), round(end / TICK)))

    def test_a_key_pressed_while_space_repeats_keeps_it_firing_until_space_comes_again(self):
        for controls in (LatchedControls(), TimedControls()):
            with self.subTest(controls.scheme):
                for t in self.REPEATING:
                    controls.feed("fire", press("space", t), False, t)
                controls.feed("right", press("d", 0.8), True, 0.8)  # steering: Space no longer repeats
                controls.feed("stop", press("s", 2.0), True, 2.0)  # nor after a stop
                self.assertEqual(self.fired_ticks(controls, 0.8, 3.0), round(2.2 / TICK))
                controls.feed("fire", press("space", 3.0), True, 3.0)  # Space again: let go, and tapped
                self.assertEqual(self.fired_ticks(controls, 3.0, 4.0), FIRE_BUFFER_TICKS)

    def test_a_bomb_tap_takes_the_repeats_too_and_clear_lets_go(self):
        controls = LatchedControls()
        for t in self.REPEATING:
            controls.feed("fire", press("space", t), False, t)
        controls.sample(0.78)
        controls.pulse("bomb")
        self.assertEqual(controls.sample(0.79), ("bomb", "fire"))
        self.assertEqual(self.fired_ticks(controls, 1.0, 2.0), TICK_RATE)
        controls.clear()  # a pause or a scene change
        self.assertEqual(self.fired_ticks(controls, 2.0, 3.0), 0)

    def test_space_let_go_before_the_other_key_or_tapped_is_no_hold(self):
        for taps in ([0.0, 0.5, 0.59], [0.0, 0.3]):  # repeats that ended 0.3 s before; two taps
            with self.subTest(taps=taps):
                controls = LatchedControls()
                for t in taps:
                    controls.feed("fire", press("space", t), False, t)
                self.fired_ticks(controls, taps[-1], 0.9)
                controls.feed("right", press("d", 0.9), True, 0.9)
                self.assertEqual(self.fired_ticks(controls, 0.9, 2.0), 0)
        controls = LatchedControls()
        controls.fire_repeats = False  # Arcade: a held Space fires once
        for t in self.REPEATING:
            controls.feed("fire", press("space", t), t == 0.0, t)
        self.assertEqual(self.fired_ticks(controls, 0.0, 0.8), FIRE_BUFFER_TICKS)
        controls.feed("right", press("d", 0.8), True, 0.8)
        self.assertEqual(self.fired_ticks(controls, 0.8, 2.0), 0)


class HeldFireGapTests(unittest.TestCase):
    """How close Space's events, and then another key, must come to read as a held Space: 0.12 s locally and 0.25 s
    over SSH (Phase 1's gap, kept there so that no hold is missed on a jittery link), in both tap schemes as
    make_controls builds them. An OS repeat every 0.09 s is a hold whenever the steer comes. Locally, taps 0.17 s
    apart or a repeat every 0.18 s are not, so fire ends with the last event's buffer; over SSH both are a hold."""

    def schemes(self):
        """(label, build, gap): latched and timed controls on a legacy terminal, locally and over SSH."""
        for where, fixture, gap in (("local", "xterm", 0.12), ("ssh", "ssh-apple-terminal", 0.25)):
            caps = capabilities(load(fixture))
            for scheme in ("latched", "timed"):
                yield f"{scheme} {where}", lambda caps=caps, scheme=scheme: make_controls(caps, scheme), gap

    def play(self, controls, spaces, steer, end=3.0):
        """Space events at `spaces` and D at `steer`, fed between samples tick by tick to `end`: the ticks that fired,
        and the first tick at or after the last Space."""
        events, fired = sorted([(t, "fire", "space") for t in spaces] + [(steer, "right", "d")]), []
        for tick in range(round(end / TICK) + 1):
            while events and events[0][0] <= tick * TICK:
                at, action, name = events.pop(0)
                controls.feed(action, press(name, at), True, at)
            if "fire" in controls.sample(tick * TICK):
                fired.append(tick)
        return fired, math.ceil(spaces[-1] / TICK - 1e-9)

    def held(self, controls, spaces, steer, end=3.0):
        """Whether fire was still on at the end (it stops 15 ticks after the last Space unless held)."""
        fired, _ = self.play(controls, spaces, steer, end)
        return fired[-1] == round(end / TICK)

    def test_the_hold_fallback_on_a_terminal_without_releases_uses_the_gap_of_where_it_runs(self):
        """--controls hold on a terminal that reports no releases falls back to latched controls, which take the
        remote flag too: quick taps then a steer are a held Space over SSH and not locally."""
        taps = [1.0, 1.17, 1.34, 1.51]  # about 6 a second, then D 0.10 s after the last
        for where, fixture in (("local", "xterm"), ("ssh", "ssh-apple-terminal")):
            with self.subTest(where):
                controls = make_controls(capabilities(load(fixture)), "hold")
                self.assertEqual(controls.scheme, "latched")
                self.play(controls, taps, 1.61)
                self.assertEqual(controls.held_fire, where == "ssh")

    def test_quick_taps_then_a_steer_are_no_hold_locally_and_fire_ends_15_ticks_after_the_last_tap(self):
        taps = [1.0, 1.17, 1.34, 1.51]  # about 6 a second, then D 0.10 s after the last
        for label, build, _ in self.schemes():
            with self.subTest(label):
                controls = build()
                fired, last = self.play(controls, taps, 1.61)
                if label.endswith("ssh"):
                    # Over SSH these taps are inside the 0.25 s gap: a held Space, as in Phase 1, so fire stays on
                    # until Space comes again (the accepted cost of not missing a hold on a jittery link; README)
                    self.assertTrue(controls.held_fire)
                    self.assertEqual(fired[-1], round(3.0 / TICK))
                else:
                    self.assertFalse(controls.held_fire)
                    self.assertEqual(fired[-1], last + FIRE_BUFFER_TICKS - 1)

    def test_a_steer_at_any_phase_of_a_held_spaces_repeat_keeps_it_firing_and_one_past_the_gap_does_not(self):
        repeating = [0.0, 0.5, 0.59, 0.68, 0.77]  # press, the OS delay, repeats every 0.09 s
        for label, build, gap in self.schemes():
            for phase in [n / 100 for n in range(10)]:  # D 0 to 0.09 s after the last repeat
                with self.subTest(label, phase=phase):
                    self.assertTrue(self.held(build(), repeating, 0.77 + phase))
            with self.subTest(label, steer="past the gap"):  # no repeat came after 0.77: Space was let go
                self.assertFalse(self.held(build(), repeating, 0.77 + gap + 0.005))

    def test_the_gaps_edges_locally_and_over_ssh(self):
        for label, build, gap in self.schemes():
            for space, steer, expected in (
                (gap - 0.005, gap - 0.005, True),  # Space's events and the steer each just inside the gap
                (gap + 0.005, 0.03, False),  # Space's events just too far apart: taps
                (0.09, gap + 0.005, False),  # the steer just too late: Space was let go
            ):
                with self.subTest(label, space=space, steer=steer):
                    spaces = [0.0, 0.5, 0.5 + space, 0.5 + 2 * space]
                    self.assertEqual(self.held(build(), spaces, spaces[-1] + steer), expected)

    def test_a_slow_key_repeat_held_and_steered_stops_the_fire_locally(self):
        # macOS KeyRepeat 12, a repeat every 0.18 s, is slower than the local gap. The accepted cost (README): fire
        # stops after a steer while Space is still down, and the player presses Space again. Over SSH it is inside
        # the 0.25 s gap, so Space held and steered keeps firing there.
        held = [0.0] + [0.525 + 0.18 * n for n in range(4)]
        for label, build, _ in self.schemes():
            for phase in (0.02, 0.10, 0.17):
                with self.subTest(label, phase=phase):
                    controls = build()
                    fired, last = self.play(controls, held, held[-1] + phase)
                    if label.endswith("ssh"):
                        self.assertTrue(controls.held_fire)
                        self.assertEqual(fired[-1], round(3.0 / TICK))
                    else:
                        self.assertFalse(controls.held_fire)
                        self.assertEqual(fired[-1], last + FIRE_BUFFER_TICKS - 1)


class PressFireTests(unittest.TestCase):
    """press_fire, an Arcade game's fire (spec decision 15): a press fires for one tick, a repeat or a held key never
    again in the tap schemes; the hold scheme fires while the key is really down."""

    def fired(self, controls, start, end):
        return sum("fire" in controls.sample(n * TICK) for n in range(round(start / TICK), round(end / TICK)))

    def test_tap_schemes_fire_one_tick_per_press_and_never_from_repeats(self):
        for controls in (LatchedControls(), TimedControls()):
            with self.subTest(controls.scheme):
                controls.press_fire = True
                controls.feed("fire", press("space", 0.0), True, 0.0)
                self.assertEqual(self.fired(controls, 0.0, 0.5), 1)  # not FIRE_BUFFER_TICKS
                for t in (0.5, 0.59, 0.68, 0.77):  # the OS repeats the held Space
                    controls.feed("fire", press("space", t), False, t)
                controls.feed("right", press("d", 0.8), True, 0.8)  # another key does not make it a hold
                self.assertEqual(self.fired(controls, 0.5, 2.0), 0)
                for t in (2.0, 2.25, 2.5):
                    controls.feed("fire", press("space", t), True, t)
                    self.assertEqual(self.fired(controls, t, t + 0.25), 1)
                controls.press_fire = False  # back to the campaign: a tap fires until the shot goes
                controls.feed("fire", press("space", 3.0), True, 3.0)
                self.assertEqual(self.fired(controls, 3.0, 4.0), FIRE_BUFFER_TICKS)

    def test_a_deferred_press_fires_at_its_time_unless_a_repeat_comes_first(self):
        for controls in (LatchedControls(), TimedControls()):
            with self.subTest(controls.scheme):
                controls.press_fire = True
                controls.feed("fire", press("space", 1.0), None, 1.0)  # maybe a held Space's first repeat
                controls.defer_fire(1.15)
                self.assertEqual(self.fired(controls, 1.0, 1.15), 0)
                self.assertEqual(self.fired(controls, 1.15, 1.5), 1)  # no repeat came: a tap, fired late
                controls.feed("fire", press("space", 2.0), None, 2.0)
                controls.defer_fire(2.15)
                controls.feed("fire", press("space", 2.09), False, 2.09)  # a repeat: it was held
                self.assertEqual(self.fired(controls, 2.0, 3.0), 0)
                controls.feed("fire", press("space", 3.0), None, 3.0)
                controls.defer_fire(3.15)
                controls.feed("fire", press("space", 3.2), True, 3.2)  # the next press, before any sample
                self.assertEqual(self.fired(controls, 3.2, 4.0), 1)
                controls.feed("fire", press("space", 4.0), None, 4.0)
                controls.defer_fire(4.15)
                controls.clear()  # a pause or a scene change forgets it
                self.assertEqual(self.fired(controls, 4.0, 5.0), 0)

    def test_hold_fires_while_the_key_is_down_and_once_for_a_tap_inside_a_tick(self):
        controls = HoldControls()
        controls.press_fire = True
        controls.feed("fire", press("space"), True, 0.0)
        controls.feed("fire", release("space"), False, 0.005)
        self.assertEqual([controls.sample(n * TICK) for n in (1, 2, 3)], [("fire",), (), ()])
        controls.feed("fire", press("space"), True, 1.0)
        controls.feed("fire", repeat("space"), False, 1.02)
        self.assertEqual([controls.sample(1.0 + n * TICK) for n in range(4)], [("fire",)] * 4)
        controls.fired()
        self.assertEqual(controls.sample(1.1), ("fire",))  # still down
        controls.feed("fire", release("space"), False, 1.2)
        self.assertEqual(controls.sample(1.2), ())

    def test_the_session_sets_it_from_the_active_game(self):
        current = session()
        self.assertFalse(current.controls.press_fire)
        current.mode = "arcade"
        current.start()
        self.assertTrue(current.controls.press_fire)
        current.set_scheme(HoldControls(), True)
        self.assertTrue(current.controls.press_fire)
        current.mode = "classic"
        current.start()
        self.assertFalse(current.controls.press_fire)


class TimedControlsTests(unittest.TestCase):
    def feed(self, controls, action, name, times):
        for t in times:
            controls.feed(action, press(name), True, t)

    def test_a_tap_latches_and_a_hold_ends_when_its_repeats_stop(self):
        controls = TimedControls()
        self.feed(controls, "right", "d", [0.0])
        self.assertEqual(controls.sample(1.0), ("right",))  # a tap: latched
        controls.feed("left", press("a"), True, 1.1)
        self.assertEqual(controls.sample(1.1), ())  # the opposite tap stops it
        self.feed(controls, "left", "a", [2.0, 2.5, 2.55, 2.6])  # press, OS delay, repeats: a hold
        self.assertEqual(controls.sample(2.7), ("left",))
        self.assertEqual(controls.sample(2.6 + controls.repeat.burst_gap + 0.01), ())

    def test_a_hold_rolls_over_to_the_other_way(self):
        controls = TimedControls()
        self.feed(controls, "left", "a", [0.0, 0.5, 0.55, 0.6])
        self.feed(controls, "right", "d", [0.62])
        self.assertEqual(controls.sample(0.65), ("right",))

    def test_firing_while_moving_keeps_the_direction_latched(self):
        controls = TimedControls()
        self.feed(controls, "right", "d", [0.0, 0.5, 0.55, 0.6])
        self.feed(controls, "fire", "space", [0.62, 1.1, 1.15])  # the OS now repeats space
        self.assertEqual(controls.sample(1.5), ("fire", "right"))
        self.feed(controls, "stop", "s", [1.6])
        self.assertEqual(controls.sample(1.6), ("fire",))

    def test_a_bomb_while_moving_keeps_the_direction_latched(self):
        controls = TimedControls()
        self.feed(controls, "right", "d", [0.0, 0.5, 0.55, 0.6])  # a hold
        controls.pulse("bomb")  # B: the OS now repeats B, not D, though D may still be down
        self.assertEqual(controls.sample(1.5), ("bomb", "right"))
        self.assertEqual(controls.sample(1.6), ("right",))
        self.feed(controls, "stop", "s", [1.7])
        self.assertEqual(controls.sample(1.7), ())

    def test_remote_timings_and_the_learned_burst_gap(self):
        self.assertEqual(TimedControls(remote=True).repeat.burst_gap, 0.20)
        controls = TimedControls()
        self.feed(controls, "right", "d", [0.0] + [0.5 + 0.03 * n for n in range(12)])
        self.assertLess(controls.repeat.burst_gap, 0.15)  # learned from these keys' own repeats


class UnlatchTests(unittest.TestCase):
    """Spec 16.2: on the upgrade card and the continue prompt the Session unlatches after each tick that held a
    direction, so every tap is an edge, even of the way already latched; fire is never touched."""

    def test_latched_drops_the_direction_as_a_stop_would_and_keeps_fire(self):
        controls = LatchedControls()
        controls.feed("right", press("d", 0.0), True, 0.0)
        controls.feed("fire", press("space", 0.0), True, 0.0)
        self.assertEqual(controls.sample(0.0), ("fire", "right"))
        controls.feed("right", press("d", 0.3), True, 0.3)  # without unlatch, a second tap the same way does nothing
        self.assertEqual(controls.sample(0.3), ("fire", "right"))
        controls.unlatch()
        self.assertEqual(controls.sample(0.31), ("fire",))  # the fire buffer still runs
        controls.fired()
        controls.feed("right", press("d", 0.6), True, 0.6)  # the same way again: latched anew, a new edge
        self.assertEqual(controls.sample(0.6), ("right",))
        controls.unlatch()
        controls.feed("right", press("d", 0.62), False, 0.62)  # an OS repeat of a held D is no tap
        self.assertEqual(controls.sample(0.63), ())

    def test_timed_ends_the_hold_window_and_keeps_fire(self):
        controls = TimedControls()
        for t in (0.0, 0.5, 0.55, 0.6):  # press, the OS delay, repeats: a hold
            controls.feed("left", press("a", t), t == 0.0, t)
        controls.feed("fire", press("space", 0.61), True, 0.61)
        self.assertEqual(controls.sample(0.62), ("fire", "left"))
        controls.unlatch()
        self.assertEqual((controls.sample(0.63), controls.held), (("fire",), False))
        controls.fired()
        controls.feed("left", press("a", 1.5), True, 1.5)  # a tap the same way: a new latch
        self.assertEqual(controls.sample(1.5), ("left",))
        controls.unlatch()
        controls.feed("left", press("a", 1.53), False, 1.53)  # a key still held latches again with its repeat
        self.assertEqual(controls.sample(1.54), ("left",))

    def test_hold_does_nothing_since_a_release_ends_the_key(self):
        controls = HoldControls()
        controls.feed("right", press("d"), True, 0.0)
        controls.feed("fire", press("space"), True, 0.0)
        controls.unlatch()
        self.assertEqual(controls.sample(0.0), ("fire", "right"))  # both still down
        controls.feed("right", release("d"), False, 0.1)
        controls.unlatch()
        self.assertEqual(controls.sample(0.1), ("fire",))

    def test_every_scheme_make_controls_builds_has_it(self):
        for caps, wanted in ((capabilities(load("ghostty")), "auto"), (capabilities(load("xterm")), "auto")):
            for scheme in (wanted, "latched", "timed"):
                make_controls(caps, scheme).unlatch()


class MakeControlsTests(unittest.TestCase):
    def test_auto_hold_latched_and_timed_on_each_kind_of_terminal(self):
        kitty, legacy = capabilities(load("ghostty")), capabilities(load("xterm"))
        physical = Keymap.preset("wasd-physical")
        cases = [
            (kitty, "auto", None, HoldControls, ""),
            (kitty, "hold", None, HoldControls, ""),
            (kitty, "latched", None, LatchedControls, ""),
            (kitty, "timed", None, TimedControls, ""),
            (kitty, "auto", physical, HoldControls, PHYSICAL_NOTE),  # flags 11 lack 4
            (replace(kitty, kitty_flags=15), "auto", physical, HoldControls, ""),
            (legacy, "auto", None, LatchedControls, ""),
            (legacy, "hold", None, LatchedControls, HOLD_NOTE),
            (legacy, "timed", physical, TimedControls, PHYSICAL_NOTE),
            (legacy, "latched", physical, LatchedControls, PHYSICAL_NOTE),
        ]
        for caps, wanted, keymap, expected, note in cases:
            with self.subTest(family=caps.family, controls=wanted, physical=keymap is not None):
                controls = make_controls(caps, wanted, keymap)
                self.assertIs(type(controls), expected)
                self.assertEqual(controls.note, note)
        self.assertIs(type(make_controls(kitty)), HoldControls)  # --controls auto, no keymap: no note
        remote = make_controls(capabilities(load("ssh-apple-terminal")), "timed")
        self.assertTrue(remote.repeat.remote)


class PlayKeyTests(unittest.TestCase):
    """The keymap, the Router and each scheme together, on a real Session."""

    def schemes(self):
        yield "hold", HoldControls(), True
        yield "latched", LatchedControls(), False
        yield "timed", TimedControls(), False

    def test_azerty_zqsd_fire_left_stop_and_right(self):
        for scheme, controls, certain in self.schemes():
            with self.subTest(scheme):
                current = session()
                current.set_scheme(controls, certain)
                seen, t = [], 1.0
                for name in ("z", "q", "s", "d"):
                    current.handle(press(name, t, certain=certain), t)
                    if certain:
                        current.handle(release(name, t + 0.05), t + 0.05)
                    seen.append(set(current.controls.sample(t + 0.01)))
                    if "fire" in seen[-1]:
                        current.controls.fired()  # as the engine's shot does
                    t += 1.0
                self.assertEqual(seen, [{"fire"}, {"left"}, set(), {"right"}])

    def test_q_in_play_never_leaves_play_with_any_preset(self):
        for preset in PRESETS:
            for scheme, controls, certain in self.schemes():
                with self.subTest(preset=preset, scheme=scheme):
                    current = session(keymap=Keymap.preset(preset))
                    current.set_scheme(controls, certain)
                    for n in range(50):
                        current.handle(press("q", n * 0.02, certain=certain), n * 0.02)
                        current.handle(press("q", n * 0.02 + 0.01, mods=SHIFT, certain=certain), n * 0.02 + 0.01)
                    self.assertEqual((current.scene, current.quit), ("playing", False))


class VirtualInputTests(unittest.TestCase):
    def collect(self, vin, times):
        return [item for t in times for item in vin.bytes_until(t)]

    def test_legacy_keys_are_a_press_then_os_repeats_and_no_release(self):
        vin = VirtualInput("legacy", FAST)
        vin.press("d", 0.0)
        self.assertTrue(vin.down("d"))
        vin.release("d", 0.4)
        self.assertFalse(vin.down("d"))
        vin.tap("space", 1.0)
        out = self.collect(vin, [2.0])
        times = [round(t, 3) for t, data in out if data == b"d"]
        self.assertEqual(times, [0.0, 0.225, 0.255, 0.285, 0.315, 0.345, 0.375])
        self.assertEqual(out[-1], (1.0, b" "))
        vin.tap("a", 1.5)  # in the past: it happens now
        self.assertEqual(vin.bytes_until(3.0), [(2.0, b"a")])

    def test_kitty_keys_report_press_repeat_and_release(self):
        vin = VirtualInput("kitty", FAST)
        vin.press("a", 0.0)
        vin.release("a", 0.3)
        out = self.collect(vin, [1.0])
        self.assertEqual(out[0], (0.0, b"\x1b[97u"))
        self.assertEqual({data for _, data in out[1:-1]}, {b"\x1b[97;1:2u"})
        self.assertEqual(out[-1], (0.3, b"\x1b[97;1:3u"))

    def test_any_slicing_of_time_gives_the_bytes_of_one_whole_run(self):
        holds = [("d", 0.0, 1.2), ("space", 0.5, 0.56), ("a", 0.9, 1.0), ("s", 1.5, 1.6)]
        for protocol in ("legacy", "kitty"):
            with self.subTest(protocol):
                whole = stream(holds, FAST, kitty=protocol == "kitty")
                vin = VirtualInput(protocol, FAST)
                for name, down, up in holds:
                    vin.press(name, down)
                    vin.release(name, up)
                sliced = self.collect(vin, [n * TICK for n in range(1, 130)])
                self.assertEqual(sliced, whole)
        d_repeats = [t for t, data in stream(holds, FAST, kitty=False) if data == b"d"]
        self.assertAlmostEqual(d_repeats[-1], 0.495)  # space took the repeat away from d for good

    def test_a_driven_keyboard_sliced_by_ticks_gives_the_bytes_of_the_whole_run(self):
        """Random intents and pulses for 20 s: ticks of bytes_until() equal one simkeys.stream() of every hold, and
        the holds kept stay few (a key held for the whole run used to keep every later tap)."""
        for trial in range(24):
            rng = random.Random(trial)
            protocol, scheme = rng.choice(("legacy", "kitty")), rng.choice(("hold", "latched", "timed"))
            with self.subTest(trial=trial, protocol=protocol, scheme=scheme):
                vin = VirtualInput(protocol, rng.choice(list(PROFILES.values())))
                pressed = []
                press = vin.press
                vin.press = lambda name, t: (press(name, t), pressed.append(vin.holds[-1]))
                driver, held, out, kept = IntentDriver(scheme, vin), frozenset({"fire"}), [], 0
                for tick in range(1200):
                    if rng.random() < 0.05:
                        held = frozenset(name for name in ("left", "right") if rng.random() < 0.4) | {"fire"}
                    driver.intend(tick, held)
                    if rng.random() < 0.01:
                        driver.pulse("bomb", tick)
                    out += vin.bytes_until((tick + 1) * TICK)
                    kept = max(kept, len(vin.holds))
                end = 1200 * TICK
                holds = [(name, down, min(up, end)) for name, down, up in pressed if up >= down]
                whole = [item for item in stream(holds, vin.profile, kitty=protocol == "kitty") if item[0] < end]
                self.assertEqual(out, whole)
                self.assertLessEqual(kept, 6)

    def test_a_release_before_a_press_still_to_come_cancels_it(self):
        vin = VirtualInput("kitty", FAST)
        driver = IntentDriver("hold", vin)
        driver.intend(0, frozenset({"fire"}))
        driver.pulse("bomb", 30)  # Space is let go for the B tap and pressed again after it, at 0.57 s
        driver.intend(31, frozenset())  # the bot stops firing before that
        sent = vin.bytes_until(2.0)
        space = [data for _, data in sent if data.startswith(b"\x1b[32") and not data.endswith(b":2u")]
        self.assertEqual(space, [encode(press("space"), kitty=True), encode(release("space"), kitty=True)])
        self.assertFalse(vin.down("space"))

    def test_keys_pressed_on_the_fly_arrive_after_the_ssh_link_in_order(self):
        link = Link(slot=0.02, jitter=0.03)
        with self.assertRaises(ValueError):
            VirtualInput("legacy", FAST, ssh=link)
        vin = VirtualInput("legacy", FAST, ssh=link, rng=random.Random(3))
        out = []
        for n in range(1, 120):
            if n % 10 == 0:
                vin.tap("d" if n % 20 else "a", n * TICK)
            out += vin.bytes_until((n + 1) * TICK)
        arrivals = [arrival for arrival, _ in out]
        self.assertEqual(arrivals, sorted(arrivals))
        self.assertTrue(all(abs(arrival / 0.02 - round(arrival / 0.02)) < 1e-6 for arrival in arrivals))
        sent = [n * TICK for n in range(10, 120, 10)]
        self.assertEqual([data for _, data in out], [b"d" if round(t * 60) % 20 else b"a" for t in sent])
        self.assertTrue(all(0 <= arrival - t <= 0.05 + 1e-9 for arrival, t in zip(arrivals, sent)))


class IntentDriverTests(unittest.TestCase):
    def keys(self, scheme, intents, until=3.0):
        """Run per-tick intents {tick: held}; returns [(time, name, kind)] of the keys the driver sent."""
        vin = VirtualInput("kitty", FAST)  # kitty bytes show releases too
        driver = IntentDriver(scheme, vin)
        held = frozenset()
        for tick in range(int(until * TICK_RATE)):
            held = intents.get(tick, held)
            driver.intend(tick, held)
        vin.release("space", until)
        return [(round(t, 3), data) for t, data in vin.bytes_until(until + 1) if not data.endswith(b":2u")]

    def test_hold_and_timed_hold_keys_down(self):
        for scheme in ("hold", "timed"):
            with self.subTest(scheme):
                sent = self.keys(scheme, {30: frozenset({"right"}), 60: frozenset({"left"}), 90: frozenset()})
                d, a = encode(press("d"), kitty=True), encode(press("a"), kitty=True)
                d_up, a_up = encode(release("d"), kitty=True), encode(release("a"), kitty=True)
                self.assertEqual(sent, [(0.5, d), (1.0, d_up), (1.0, a), (1.5, a_up)])

    def test_latched_taps_a_way_taps_s_to_stop_and_taps_twice_to_reverse(self):
        intents = {30: frozenset({"right"}), 60: frozenset({"left"}), 90: frozenset(), 95: frozenset({"right"})}
        sent = [(t, data) for t, data in self.keys("latched", intents) if not data.endswith(b":3u")]
        presses = [(t, data.decode()) for t, data in sent]
        expected = [(0.5, "d"), (1.0, "a"), (1.12, "a"), (1.5, "s"), (1.767, "d")]  # the last waits for SETTLE
        self.assertEqual(presses, [(t, f"\x1b[{ord(name)}u") for t, name in expected])

    def test_fire_holds_space_and_a_key_pressed_before_its_repeats_presses_it_again(self):
        space, space_up = encode(press("space"), kitty=True), encode(release("space"), kitty=True)
        cases = [  # (scheme, intents, Space pressed at, let go at): FAST repeats Space from 0.225 s after its press
            ("latched", {10: {"fire"}, 60: {"fire", "right"}}, [round(10 * TICK, 3)], [2.0]),  # it was repeating
            ("latched", {60: {"fire", "right"}}, [1.0, 1.07], [1.0, 2.0]),  # the tap ends 1.06 s
            ("timed", {60: {"fire", "right"}}, [1.0, 1.01], [1.0, 2.0]),  # the move key is held down
            ("hold", {60: {"fire", "right"}}, [1.0], [2.0]),  # releases say Space is down
        ]
        for scheme, intents, pressed, let_go in cases:
            with self.subTest(scheme=scheme, intents=intents):
                sent = self.keys(scheme, {tick: frozenset(held) for tick, held in intents.items()}, until=2.0)
                self.assertEqual([t for t, data in sent if data == space], pressed)
                self.assertEqual([t for t, data in sent if data == space_up], let_go)
        vin = VirtualInput("legacy", FAST)
        driver = IntentDriver("hold", vin)
        driver.pulse("bomb", 60)
        self.assertEqual(vin.bytes_until(2.0), [(1.0, b"b")])
