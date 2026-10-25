"""Phase 5's effects (spec 2, 12.1): what sets them off, the burst, debris, the kick, the starfield, drifting popups,
the levels and the flash limiter; reduced motion and level `off` drawing no spark; the ten effect scenes; and the
shared bot runs (slow) that check the kick rate and the UFO's recognition."""

import copy
import itertools
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_invaders import fx
from terminal_invaders.arena import PixelArena
from terminal_invaders.campaign.tables import ACTS, BOSSES, COMBO, PICKUP, PROFILES, SCORING, UFO
from terminal_invaders.model import HEIGHT, WIDTH, Config, Effect, Pickup, Player
from terminal_invaders.replay import Playback
from terminal_invaders.sprites import PICKUPS, RING
from terminal_invaders.term.cells import CellBuffer
from terminal_invaders.theme import SLOT, resolve_theme
from terminal_invaders.ui import Painter
from tests.support import fx_scenes
from tests.support.frames import options

TOOLS = Path(__file__).resolve().parents[1] / "tools"
TRUECOLOR = resolve_theme(SimpleNamespace(color="truecolor", theme="dark", paint_background=True))
MONO = resolve_theme(SimpleNamespace(color="mono", theme="dark", paint_background=False))
SIZES = {1: (80, 24), 2: (120, 44), 3: (180, 64), 4: (240, 84), 6: (360, 124)}
TTL0 = {"+10": 30, "+300": 30, "+5,000": 90, "PHASE BREAK": 60, "HIT": 45}


def effect(text: str, age: int, x: float = 30.0, y: float = 10.0) -> Effect:
    """An Effect of the game first seen `age` ticks ago (an effect is first seen at age 1)."""
    return Effect(x, y, text, TTL0.get(text, 30) - age)


def game(*effects, tick: int = 500, mode: str = "classic"):
    """A stand-in game holding the Effects and an empty field (the effect functions read nothing else but the tick
    and config; the arena draws the rest), as tests/test_arena.py's stage() builds one."""
    field = dict(act=1, player=Player(), aliens=[], shots=[], pickups=[], ufo=None, boss=None, bunkers={}, hazards=[])
    return SimpleNamespace(effects=list(effects), tick=tick, config=Config(mode=mode), phase="playing", **field)


def made(kind: str, age: int, **where) -> Effect:
    """The Effect of a source of `kind` first seen `age` ticks ago (a UFO kill on the UFO's row)."""
    texts = {"kill": "+10", "ufo": "+300", "boss": "+5,000", "phase": "PHASE BREAK", "hit": "HIT"}
    if kind == "ufo":
        where.setdefault("y", UFO.y)
    return effect(texts[kind], age, **where)


def at(kind: str, age: int, **where) -> fx.Source:
    (found,) = fx.sources(game(made(kind, age, **where)))
    return found


def drawn(current, k: int, theme=TRUECOLOR, painter=None, **settings):
    """The arena alone drawn for a game (as tests/test_arena.py draws it), on a block of scale k."""
    if painter is None:
        painter = Painter(CellBuffer(*SIZES[k]), options(**settings), theme, None)
        assert painter.layout.k == k
    painter.view.draw(painter, SimpleNamespace(pilot=SimpleNamespace(game=current)))
    return painter


def frame(painter) -> tuple:
    layer = getattr(painter.view, "layer", None)
    return tuple(painter.screen.chars), tuple(painter.screen.styles), bytes(layer.px) if layer else b""


def slots_drawn(current, k: int, **settings) -> set[int]:
    """Every slot the arena draws in: the k = 1 arena's text calls, or the pixel layer's roles."""
    painter = Painter(CellBuffer(*SIZES[k]), options(**settings), TRUECOLOR, None)
    if k > 1:
        return set(drawn(current, k, painter=painter).view.layer.px)
    seen, text = set(), painter.text

    def recording(y, x, string, color=6, *rest, **named):  # Painter.text's signature
        seen.add(color)
        return text(y, x, string, color, *rest, **named)

    painter.text = recording
    drawn(current, k, painter=painter)
    return seen


def slots_shown(current, k: int, **settings) -> set[int]:
    """Every slot left on the finished screen, as fg or bg (FX-4): what was drawn and not drawn over."""
    styles = drawn(current, k, **settings).screen.styles
    return {st & 255 for st in styles} | {st >> 8 & 255 for st in styles}


# ---- what sets an effect off (spec 2.2) ----


def test_recognition():
    pickups = [kind.upper() for kind in PICKUPS]
    cases = [
        (Effect(30, 5, "+10"), ("kill", 30)),
        (Effect(30, 5, "+1000"), ("kill", 30)),  # an enemy kill never has a comma, whatever it pays
        (Effect(30, 5, "+300"), ("kill", 30)),  # a UFO's points off the UFO's row
        (Effect(30, UFO.y, "+10"), ("kill", 30)),  # golden-classic-normal's tick 16126: an entering scout
        (Effect(30, UFO.y, "+150"), ("ufo", 30)),
        (Effect(30, UFO.y, f"+{UFO.high * COMBO.most}"), ("ufo", 30)),
        (Effect(30, 4, "+5,000", 90), ("boss", 90)),
        (Effect(30, 4, "+10,000", 90), ("boss", 90)),
        (Effect(30, 4, "PHASE BREAK", 60), ("phase", 60)),
        (Effect(30, 18, "HIT", 45), ("hit", 45)),
    ]
    cases += [(Effect(30, 18, text), None) for text in (RING, "BOMB READY", "+", "+x", "EXTRA", *pickups)]
    for case, want in cases:
        assert fx.recognise(case) == want, case


def test_ufo_points_are_what_a_ufo_kill_can_pay():
    assert fx.UFO_POINTS == {points * combo for points in (UFO.high, UFO.low) for combo in range(1, COMBO.most + 1)}
    assert min(fx.UFO_POINTS) == UFO.low and max(fx.UFO_POINTS) == UFO.high * COMBO.most


@pytest.mark.parametrize("difficulty", sorted(PROFILES))
@pytest.mark.parametrize("loop", [1, 2, 3])
@pytest.mark.parametrize("act", ACTS, ids=lambda act: act.roman)
def test_every_boss_kill_shows_a_comma(act, loop, difficulty):
    """campaign/game.py _boss_killed pays SCORING.boss x act (+ boss_perfect x act without a hit) and shows it as
    f"+{points:,}": at every act, loop (an Endless loop keeps the act's number) and difficulty that is 1,000 or more,
    so its popup has the comma that names a boss kill."""
    assert act.boss in BOSSES
    for perfect in (False, True):
        points = SCORING.boss * act.number + (SCORING.boss_perfect * act.number if perfect else 0)
        assert points >= 1000
        assert fx.recognise(Effect(30, 4, f"+{points:,}", 90)) == ("boss", 90)


def test_sources_birth_age_and_their_table():
    tick = 700
    found = fx.sources(game(effect("+10", 1), effect("HIT", 3, y=18), effect(RING, 2), effect("+5,000", 4), tick=tick))
    assert [(s.kind, s.age, s.birth) for s in found] == [("kill", 1, 699), ("hit", 3, 697), ("boss", 4, 696)]
    hit = found[1]
    assert (hit.x, hit.y) == (30, 19)  # HIT is drawn over the ship: its burst is centred on the ship
    rows = {kind: at(kind, 3) for kind in ("kill", "ufo", "boss", "phase", "hit")}
    table = {kind: (s.size, s.dots, s.kick) for kind, s in rows.items()}
    assert table == {
        "kill": ("small", 8, 0),
        "ufo": ("big", 12, 0),
        "boss": ("big", 16, 10),
        "phase": ("big", 12, 6),
        "hit": ("big", 12, 6),
    }
    assert {kind: s.debris for kind, s in rows.items() if kind != "kill"} == {
        "ufo": 14,
        "boss": 24,
        "phase": 12,
        "hit": 12,
    }
    kills = {at("kill", 3, x=x / 4, y=y / 4).debris for x in range(240) for y in range(0, 80, 7)}
    assert kills == set(range(6, 11))  # 6 + mix(DEBRIS, b, ix, iy) % 5
    first = at("kill", 3, x=12.25, y=4.5)
    assert first.debris == 6 + fx.mix(fx.DEBRIS, first.birth, 49, 18) % 5


# ---- the burst (spec 2.3) ----


def test_burst_frames_by_age():
    for age in range(0, 16):
        shown = fx.bursts(game(effect("+10", age)), 2, fx.FULL)
        want = None if age > 12 else (max(1, age) - 1) // 4
        assert [b.frame for b in shown] == ([] if want is None else [want]), age
    assert fx.bursts(game(effect("+10", 2)), 2, fx.OFF) == []
    assert fx.BURST == {"small": ("&", "; ;", ",   ,"), "big": ("&&&", ";   ;", ",     ,")}


@pytest.mark.parametrize("kind, dots, radii", [("kill", 8, (0.5, 1.0, 1.5)), ("boss", 16, (1.0, 2.0, 3.0))])
def test_burst_rings(kind, dots, radii):
    for frame, radius in enumerate(radii):
        source = at(kind, 1 + 4 * frame)
        (burst,) = fx.bursts(game(effect(source.text, source.age)), 2, fx.FULL)
        offsets = fx.ring_offsets(burst.size, burst.frame, burst.dots)
        assert len(offsets) == dots and burst.frame == frame
        assert all(math.isclose(math.hypot(ox, 2 * oy), radius) for ox, oy in offsets)  # rows halved


@pytest.mark.parametrize("text, glyphs", [("+10", fx.BURST["small"]), ("+5,000", fx.BURST["big"])])
def test_the_k1_burst_glyphs(text, glyphs):
    for frame, row in enumerate(glyphs):
        painter = Painter(CellBuffer(*SIZES[1]), options(effects="full"), TRUECOLOR, None)
        painter.link = fx.Link(remote=True)  # no kick: the boss kill's would move the field a column
        drawn(game(effect(text, 1 + 4 * frame, x=30, y=10)), 1, painter=painter)
        line = painter.screen.row_text(painter.top + 2 + 10)[painter.left : painter.left + WIDTH]
        start = 30 - len(row) // 2
        assert all(line[start + i] == char for i, char in enumerate(row) if char != " ")


# ---- debris (spec 2.4) ----


def test_debris_count_life_and_fade():
    for kind in ("kill", "ufo", "boss", "phase", "hit"):
        assert fx.debris(game(made(kind, 4)), 2, fx.FULL) == []  # none before age 5
        source = at(kind, 5)
        flights = fx.flights(source.birth, math.floor(4 * source.x), math.floor(4 * source.y), source.debris)
        assert len(flights) == source.debris and all(16 <= life <= 24 for *_, life in flights)
        assert all(0.05 <= speed <= 0.13 for _, _, speed, _ in flights)
        assert len(fx.debris(game(made(kind, 5)), 3, fx.FULL)) == source.debris
    source = at("boss", 10)
    cos, sin, speed, life = fx.flights(source.birth, 120, 40, 24)[0]
    e = 10 - 4
    reach = speed * e * (1 - e / (2 * life))
    x, y, role = fx.debris(game(effect("+5,000", 10)), 2, fx.FULL)[0]
    assert math.isclose(x, 30 + cos * reach) and math.isclose(y, 10 + 0.5 * sin * reach) and role == "spark"
    roles = {age: {role for *_, role in fx.debris(game(effect("+5,000", age)), 2, fx.FULL)} for age in range(5, 30)}
    assert roles[5] == {"spark"} and roles[24] == {"star"} and roles[29] == set()  # life ends by 24 ticks of flight
    counts = [len(fx.debris(game(effect("+5,000", age)), 2, fx.FULL)) for age in range(5, 30)]
    assert counts[0] == 24 and counts[-1] == 0
    assert all(a >= b for a, b in itertools.pairwise(counts))  # points only end


def test_debris_is_clipped_capped_and_never_at_k1_or_in_mono():
    corner = fx.debris(game(effect("+5,000", 20, x=0, y=0)), 2, fx.FULL)
    assert 0 < len(corner) < 24
    assert all(-0.5 <= x < WIDTH - 0.5 and -0.5 <= y < HEIGHT - 0.5 for x, y, _ in corner)
    many = [effect("+5,000", age, x=10 + 10 * i) for i, age in enumerate((6, 7, 8, 9))]  # 4 x 24 points
    capped = fx.debris(game(*many), 2, fx.FULL)
    assert len(capped) == fx.DEBRIS_CAP == 64
    youngest = fx.debris(game(many[0]), 2, fx.FULL)
    assert capped[:24] == youngest  # the youngest source's points first
    assert fx.debris(game(*many), 1, fx.FULL) == []
    assert fx.debris(game(*many), 2, fx.level(options(effects="full"), None, game(), mono=True)) == []


@pytest.mark.parametrize("k", [2, 3, 4, 6])
def test_the_same_state_gives_the_same_pixels(k):
    """Nothing is kept between frames (spec 2.1): a game drawn again, after other frames and on a new painter, at
    any scale, draws the same pixels."""
    scene, other = fx_scenes.game("fx-boss-debris"), fx_scenes.game("fx-ufo-kill")
    first = drawn(scene, k, effects="full")
    once = frame(first)
    drawn(other, k, painter=first)
    assert frame(drawn(copy.deepcopy(scene), k, painter=first)) == once
    assert frame(drawn(scene, k, effects="full")) == once
    assert SLOT["spark"] in once[2]


# ---- the starfield (spec 2.6) ----


def test_stars_fall_wrap_and_never_reach_the_ground_row():
    assert len(fx.FIELD) == 32 and [layer for *_, layer in fx.FIELD] == [0] * 20 + [1] * 12
    for x, y0, _ in fx.FIELD:
        assert 0 <= x < 60 and 0 <= y0 < 19
    for tick in range(0, 2 * 2280, 13):
        for x, y, _ in fx.stars(tick, fx.FULL):
            assert 0 <= x < 60 and 0 <= y < 19
    for (x, y, layer), (x2, y2, _) in zip(fx.stars(100, fx.FULL), fx.stars(160, fx.FULL), strict=True):
        assert x == x2 and math.isclose((y2 - y) % 19, (0.5, 1.0)[layer])  # rows a second, by layer
    for (_, y, _), (_, y2, _) in zip(fx.stars(100, fx.FULL), fx.stars(100 + 2280, fx.FULL), strict=True):
        assert math.isclose(y, y2, abs_tol=1e-9) or math.isclose(abs(y - y2), 19, abs_tol=1e-9)  # 38 s: both layers
    assert fx.stars(900, fx.CALM) == fx.stars(0, fx.FULL) and fx.stars(900, fx.OFF) == []


@pytest.mark.parametrize("k", [1, 2, 3])
def test_stars_keep_the_ground_row_clean(k):
    for tick in range(0, 2400, 97):
        full, off = drawn(game(tick=tick), k, effects="full"), drawn(game(tick=tick), k)
        if k == 1:
            row = full.top + 2 + HEIGHT - 1
            assert full.screen.row_text(row) == off.screen.row_text(row) and "." not in full.screen.row_text(row)
        else:
            w, ground = full.view.layer.w, (HEIGHT - 1) * 2 * k
            assert full.view.layer.px[ground * w :] == off.view.layer.px[ground * w :]


# ---- the kick (spec 2.5) ----


def test_the_kick():
    for kind, ticks in (("boss", 10), ("phase", 6), ("hit", 6), ("kill", 0), ("ufo", 0)):
        for age in range(0, 14):
            source = at(kind, age)
            dx = fx.kick(game(made(kind, age)), fx.FULL, None)
            want = (1 if fx.mix(fx.KICK, source.birth) & 1 else -1) if 1 <= age <= ticks else 0
            assert dx == want, (kind, age)
    boss = game(effect("+5,000", 3))
    assert fx.kick(boss, fx.CALM, None) == 0 and fx.kick(boss, fx.FULL._replace(kick=False), None) == 0
    assert fx.kick(boss, fx.FULL, fx.FlashLimiter(limit=0)) == 0
    births = {b: game(effect("+5,000", 3), effect("HIT", 2, y=18), tick=b) for b in range(400, 440)}
    signs = {fx.kick(both, fx.FULL, None) for both in births.values()}
    assert signs == {-1, 1}  # the direction comes from the birth tick's hash
    for tick, both in births.items():  # the youngest source kicks
        assert fx.kick(both, fx.FULL, None) == (1 if fx.mix(fx.KICK, tick - 2) & 1 else -1)


def test_shift():
    px = bytearray(range(1, 9))  # 4 x 2
    assert fx.shift(px, 4, 0) is px
    assert fx.shift(px, 4, 1) == bytes([0, 1, 2, 3, 0, 5, 6, 7])
    assert fx.shift(px, 4, -1) == bytes([2, 3, 4, 0, 6, 7, 8, 0])
    assert px == bytearray(range(1, 9))


@pytest.mark.parametrize("dx", [-1, 0, 1])
def test_overlays_move_with_the_kick_and_clip(dx):
    painter = Painter(CellBuffer(*SIZES[3]), options(), TRUECOLOR, None)
    view = PixelArena(3, TRUECOLOR)
    top, left = painter.top + 2, painter.left
    view.overlay(painter, 30, 10, "+50", 7, dx)
    line = painter.screen.row_text(top + 31)
    assert line.index("+50") == left + math.floor(30.5 * 3) - 1 + dx
    view.overlay(painter, 0, 12, "+150", 7, dx, up=1)  # at the field's left edge: clipped to the arena
    assert painter.screen.row_text(top + 36)[left : left + 5] == {-1: "50   ", 0: "150  ", 1: "+150 "}[dx]
    view.overlay(painter, 30, 0, "HIT", 7, dx, up=2)  # never above the field's top row
    assert "HIT" in painter.screen.row_text(top) and "HIT" not in painter.screen.row_text(top - 1)


# ---- drifting popups (spec 2.7) ----


def test_rise():
    for age in range(0, 31):
        kill = effect("+10", age)
        assert fx.rise(kill, 2, fx.FULL) == min(2, age // 10)
        assert fx.rise(kill, 3, fx.CALM) == 0 and fx.rise(kill, 2, fx.OFF) == 0
        assert fx.rise(kill, 1, fx.FULL) == fx.rise(kill, 1, fx.CALM) == 1 and fx.rise(kill, 1, fx.OFF) == 0
    assert fx.rise(effect("+5,000", 25), 3, fx.FULL) == 2
    for text in ("HIT", "PHASE BREAK", "BOMB READY", "SHIELD", RING):
        assert fx.rise(effect(text, 20), 2, fx.FULL) == fx.rise(effect(text, 20), 1, fx.FULL) == 0


@pytest.mark.parametrize("k", [1, 2, 3])
def test_popups_drift_in_the_arena(k):
    for age, up in ((1, 0), (10, 1), (25, 2)):
        painter = drawn(game(effect("+10", age, x=30, y=10)), k, effects="full")
        top = painter.top + 2
        row = next(row for row in range(HEIGHT * k) if "+10" in painter.screen.row_text(top + row))
        assert row == (9 if k == 1 else math.floor(10.5 * k) - up), (age, row)
    painter = drawn(game(effect("+10", 25, x=30, y=0)), k, effects="full")
    assert "+10" in painter.screen.row_text(painter.top + 2)  # never above the field's top row


# ---- levels (spec 2.8) ----


def expected_level(effects, reduced, link, mono, arcade) -> fx.Level:
    """Spec 2.8's table and order, written out again."""
    if reduced or arcade or effects not in ("full", "calm"):
        return fx.Level("off", False, False, "none", False)
    link = link or fx.Link()
    name = "calm" if effects == "full" and link.congested else effects
    kick = name == "full" and not (link.remote or link.clock)
    stars = "off" if mono else ("move" if name == "full" else "still")
    return fx.Level(stars, True, not mono, "drift" if name == "full" else "offset", kick)


def test_level_for_every_combination():
    links = [None, *(fx.Link(*flags) for flags in itertools.product((False, True), repeat=3))]
    seen = set()
    for effects, reduced, link, mono, arcade in itertools.product(
        ("full", "calm", "off", None), (False, True), links, (False, True), (False, True)
    ):
        settings = dict(reduced_motion=reduced) | ({} if effects is None else dict(effects=effects))
        moment = game(mode="arcade" if arcade else "classic")
        got = fx.level(SimpleNamespace(**settings), link, moment, mono)
        assert got == expected_level(effects, reduced, link, mono, arcade), (effects, reduced, link, mono, arcade)
        seen.add(got)
    assert fx.level(None, None, None) is fx.OFF  # a test fake with no options reads off
    assert {fx.OFF, fx.FULL, fx.CALM, fx.FULL._replace(kick=False)} < seen and len(seen) == 7


# ---- the flash limiter (spec 2.9) ----


def test_flash_limiter():
    limiter = fx.FlashLimiter()
    admitted = [limiter.admit(("HIT", tick), tick) for tick in range(100, 160, 6)]  # 10 kicks in 60 ticks
    assert admitted == [True] * 3 + [False] * 7
    assert limiter.admit(("HIT", 100), 159)  # an admitted key stays admitted
    assert not limiter.admit(("HIT", 150), 159)
    assert limiter.admit(("HIT", 161), 161)  # the first left the window at tick 160
    assert not limiter.admit(("NEW", 165), 165)  # 106, 112 and 161 are in its window
    assert limiter.admit(("NEW", 50), 50)  # a lower tick (a new game, a replay restart) clears it
    assert list(limiter.admitted) == [("NEW", 50)]
    assert (fx.FLASH_LIMIT, fx.FLASH_WINDOW) == (3, 60)


def test_the_limiter_keeps_an_admitted_kick_and_refuses_a_new_one():
    """With room for one kick, a HIT kicks through its 6 ticks; a phase break born meanwhile (the youngest source,
    asked first) is refused, so the HIT's kick goes on and nothing kicks after it."""
    limiter, direction = fx.FlashLimiter(limit=1), 1 if fx.mix(fx.KICK, 200) & 1 else -1
    for tick in range(201, 212):
        shown = [Effect(30, 18, "HIT", 45 - (tick - 200))]
        if tick > 202:
            shown.append(Effect(40, 4, "PHASE BREAK", 60 - (tick - 202)))
        assert fx.kick(game(*shown, tick=tick), fx.FULL, limiter) == (direction if tick <= 206 else 0), tick
    assert list(limiter.admitted) == [("HIT", 200)]


# ---- reduced motion and level off (spec 2.8, 2.12) ----


@pytest.mark.parametrize("name", [scene.name for scene in fx_scenes.SCENES])
@pytest.mark.parametrize("k", [1, 2, 3])
def test_reduced_motion_is_level_off_and_draws_no_spark(name, k):
    scene = fx_scenes.game(name)
    full, off = (drawn(scene, k, effects=level, reduced_motion=True) for level in ("full", "off"))
    assert frame(full) == frame(off)
    for settings in (dict(effects="full", reduced_motion=True), dict(effects="off")):
        assert SLOT["spark"] not in slots_drawn(scene, k, **settings), settings
    texts = [e.text for e in scene.effects if e.text != RING]
    shown = "".join(full.screen.chars)
    assert all(text in shown for text in texts if 1 <= len(text) <= 12)  # the texts drawn still


def stepped(name: str, ticks: int):
    """A scene's game `ticks` ticks on, stepped with its tape's inputs."""
    scene = next(scene for scene in fx_scenes.SCENES if scene.name == name)
    current, tape = fx_scenes.game(name), Playback(fx_scenes.FIXTURES / scene.tape).tapes[0]
    for _ in range(ticks):
        current.step(tape.actions_at(current.tick))
    return current


@pytest.mark.parametrize("k", [1, 2, 3])
def test_level_full_draws_spark_where_a_source_shows(k):
    """In the finished frame, not only in the draw calls (FX-4): at k = 1 the arena draws texts and entities after
    the bursts. The one exception is by design: at k = 1 the ship-lost burst's first frame is on the ship, which is
    drawn over it (spec 2.1: no burst covers the ship), and the scene's tick is one the ship shows on (it is hidden
    5 ticks in 12 while invulnerable); there the burst's second frame, two ticks on, is checked."""
    for scene in fx_scenes.SCENES:
        if scene.kind and scene.age <= 12:  # a burst shows
            current = fx_scenes.game(scene.name)
            if (scene.name, k) == ("fx-ship-lost", 1):
                assert SLOT["spark"] not in slots_shown(current, k, effects=scene.effects)  # under the ship
                current = stepped(scene.name, 5 - scene.age)
            assert SLOT["spark"] in slots_shown(current, k, effects=scene.effects), scene.name


@pytest.mark.parametrize("age", range(1, 13))
def test_the_phase_break_burst_shows_at_k_1(age):
    """FX-3: at k = 1 the PHASE BREAK text is drawn on its source's row and the break's pickup, dropped on the row
    below at the same column, falls through the next; both are drawn after the bursts, so the burst goes two rows
    down and every frame of it shows."""
    current = game(made("phase", age, x=30.0, y=4.0))
    current.pickups.append(Pickup(30.0, 5.0 + PICKUP.fall * age, "shield"))  # game.py: _drops(boss.x, boss.y + 2)
    assert SLOT["spark"] in slots_shown(current, 1, effects="full")


@pytest.mark.parametrize("k", [2, 3])
def test_mono_draws_the_burst_but_no_debris_or_moving_stars(k):
    burst = drawn(game(effect("+10", 2), tick=900), k, MONO, effects="full").view.layer.px
    assert SLOT["spark"] in burst and SLOT["star"] not in burst
    debris = game(effect("+5,000", 14), tick=900)  # the burst is over, the debris flying
    assert SLOT["spark"] not in drawn(debris, k, MONO, effects="full").view.layer.px
    assert SLOT["spark"] in drawn(debris, k, effects="full").view.layer.px


def test_arcade_draws_no_effects():
    assert fx.level(options(effects="full"), None, game(mode="arcade")) is fx.OFF


# ---- the effect scenes (spec 2.12) ----


def test_the_effect_scenes_show_their_moments():
    assert len(fx_scenes.SCENES) == 10 and len({scene.name for scene in fx_scenes.SCENES}) == 10
    for scene in fx_scenes.SCENES:
        current = fx_scenes.game(scene.name)
        assert current.tick == scene.tick
        assert fx_scenes.game(scene.name) is not current  # a fresh copy each time
    wrong = fx_scenes.Scene("wrong", fx_scenes.CLASSIC, 142, "kill", 3)
    with pytest.raises(LookupError, match="not a kill at age 3"):
        fx_scenes.check(wrong, fx_scenes.flight(fx_scenes.CLASSIC)[142])


def test_the_effect_scene_entries():
    made = []
    rows = fx_scenes.entries(lambda **o: o, lambda o, g: made.append((o, g.tick)), ["t1", "t2"], ["dark"])
    assert len(rows) == 30 and {(cols, lines) for _, cols, lines, *_ in rows} == {(120, 44), (80, 24), (180, 64)}
    assert sum(len(tiers) for *_, tiers in rows) == 10 * (2 + 1 + 1)
    name, *_, build, _ = rows[-1]
    build()
    assert name == "fx-calm" and made == [({"effects": "calm"}, 142)]


# ---- the shared bot runs (spec 2.2, 2.9, 12.1) ----


@pytest.fixture(scope="module")
def bot_runs():
    """Strong bot, 10 seeds x normal and hard, to the end of wave 6 (20 games, about 40 s): for each, every
    recognised Effect as it was born (tick, kind, text, y) and whether a UFO kill made it. The UFO kill is told by
    wrapping the game's own _ufo_kill on the instance, which records and changes nothing."""
    from terminal_invaders.campaign.observe import observe
    from terminal_invaders.engine import Game

    added = str(TOOLS) not in sys.path
    if added:
        sys.path.insert(0, str(TOOLS))
    try:
        import bots

        runs = {}
        for difficulty in ("normal", "hard"):
            for seed in range(1, 11):
                current, pilot = Game(Config(difficulty, "classic", seed)), bots.make("strong", seed)
                truth, seen, births = {}, {}, []  # id -> the Effect itself, kept so that no id is reused
                ufo_kill = current._ufo_kill

                def recorded(current=current, ufo_kill=ufo_kill, truth=truth):
                    ufo_kill()
                    truth[id(current.effects[-1])] = current.effects[-1]

                current._ufo_kill = recorded
                while current.phase not in ("game_over", "victory") and current.wave <= 6 and current.tick < 10**6:
                    current.step(pilot.act(observe(current)))
                    for made in current.effects:
                        if id(made) not in seen:
                            seen[id(made)] = made
                            known = fx.recognise(made)
                            if known:
                                births.append((current.tick, known[0], made.text, made.y, id(made) in truth))
                runs[(difficulty, seed)] = births
        return runs
    finally:
        if added:
            sys.path.remove(str(TOOLS))


@pytest.mark.slow
def test_at_most_two_kicks_start_in_any_60_ticks(bot_runs):
    kicks = 0
    for key, births in bot_runs.items():
        ticks = [tick for tick, kind, *_ in births if fx.KINDS[kind][3]]
        kicks += len(ticks)
        for i, tick in enumerate(ticks):
            assert sum(1 for other in ticks[i:] if other - tick < 60) <= 2, (key, tick)
    assert kicks >= 20  # every game reaches wave 6's boss


@pytest.mark.slow
def test_the_ufo_kill_is_recognised_in_the_bot_runs(bot_runs):
    """Spec 2.2 expected no enemy to die on the UFO's row in these runs. Two do (hard seed 2 at tick 13111, a +30, and
    hard seed 5 at tick 13345, a +10: enemies killed while they enter, as golden-classic-normal's scout at tick
    16126), so fx.recognise also asks for a UFO's points (fx.UFO_POINTS). Here every `+N` is named a UFO kill
    exactly when the UFO made it, and the enemies killed on the UFO's row paid none of a UFO's points."""
    ufos, on_row = 0, []
    for key, births in bot_runs.items():
        for tick, kind, text, y, by_ufo in births:
            if "," not in text and text[0] == "+":
                assert (kind == "ufo") is by_ufo, (key, tick, text, y)
                ufos += by_ufo
                if y == UFO.y and not by_ufo:
                    on_row.append((key, tick, text))
    assert ufos >= 60
    assert len(on_row) <= 5 and not any(int(text[1:]) in fx.UFO_POINTS for *_, text in on_row), on_row
