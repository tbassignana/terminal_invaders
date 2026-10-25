"""The bots' Frame is what the arena draws (Phase 4 spec 3.5, 20.2 I row): on 20 staged scenes at k = 2, every
entity a Frame holds lies inside the pixels the arena draws for that entity.

Each scene is a real staged game (campaign.testing.staged), played by a scripted sweep (fire, and a turn every 90
ticks) with its lives held up, and looked at every SAMPLE ticks. At each look, every Frame entity is matched by id to
its model object, the object is drawn alone by the pixel arena (PixelArena at k = 2, on the game's tick, act,
shelters and boss wave, with the ship off the field), and the pixels that differ from the same arena drawn without
it are what the arena draws for it.

* A sprite (enemy, shot, pickup, boss part, the UFO, the ship, a shelter cell): the Frame's position in pixels,
  ((x + 0.5)k, (y + 0.5)2k), lies inside the box of those pixels, give or take the Frame's own rounding to 0.25 cell
  (k / 8 across, 2k / 8 down).
* A hazard covers the cells whose centre lies in its span [x0, x1) (arena.span), so in pixels it lies in
  [x0 k, (x1 + 1)k]. A beam's marks and a safe lane are drawn inside that, and a beam's rows run from its source row
  to the ground; a curtain gap's brackets and dashes are drawn round it. Gap and lane marks sit on the ground row
  whatever their y.

Left out because the arena draws nothing there by design: a boss part in state `broken` (gone), the ship while its
flicker hides it, and an entity whose centre is off the 60 x 20 field (a PINCER half entering from beyond the edge, a
pickup falling past the ground), which the arena clips. Both are counted.
"""

import copy
from collections import Counter, defaultdict
from functools import cache
from itertools import chain
from types import SimpleNamespace

import pytest

from terminal_invaders.arena import FLICKER, PixelArena
from terminal_invaders.campaign.observe import observe
from terminal_invaders.campaign.testing import staged
from terminal_invaders.model import HEIGHT, WIDTH, Config
from terminal_invaders.theme import resolve_theme

K = 2
THEME = resolve_theme(SimpleNamespace(color="truecolor", theme="dark", paint_background=True))
TICKS, SAMPLE = 1800, 45
SLACK_X, SLACK_Y = K / 8, 2 * K / 8  # the Frame's rounding to 0.25 cell, in pixels
# (difficulty, wave, boss phase): between them every enemy kind and pose, the veteran and cargo marks, every shot
# kind, every boss part and part state, the beam's three states, curtain gaps and safe lanes, pickups and the UFO.
SCENES = (
    ("normal", 1, 0),  # scouts, pellets
    ("normal", 2, 0),  # cargo, pickups
    ("normal", 3, 0),  # divers: telegraph, diving, returning
    ("normal", 5, 0),  # gunners and their tell, darts, the UFO
    ("normal", 6, 0),  # WARDEN: shielded core, turrets, guards
    ("normal", 6, 1),  # WARDEN's open core
    ("normal", 9, 0),  # weavers, shells, cracked
    ("normal", 11, 0),  # lancers, needles
    ("normal", 12, 0),  # HIVE QUEEN, clusters
    ("normal", 12, 1),  # her divers
    ("normal", 13, 0),  # bombers, slugs
    ("normal", 15, 0),  # splitters, mites
    ("hard", 16, 0),  # hunter darts, veterans
    ("normal", 17, 0),  # curtain gaps
    ("normal", 18, 0),  # DREADNOUGHT's broadside: batteries, slug salvos
    ("normal", 18, 1),  # its beam: warn, lock, live
    ("normal", 18, 2),  # its double beam and the safe lane between
    ("normal", 19, 0),  # carriers
    ("normal", 24, 1),  # MOTHERSHIP's pods
    ("normal", 24, 2),  # its core: beams, lanes, curtains, clusters
)
PAINTER = SimpleNamespace(
    options=SimpleNamespace(reduced_motion=False),
    screen=SimpleNamespace(addstr=lambda *args: None),
    top=0,
    left=0,
    colors=defaultdict(int),
    center=lambda *args, **kwargs: None,
)  # what PixelArena.draw reads of a Painter; its text (letters, popups, the boss bar) goes nowhere


class Packer:
    """The arena's half-block packer turns the layer into cells; positions are read from the layer itself."""

    def pack(self, *args) -> None:
        pass


def pixel_arena() -> PixelArena:
    view = PixelArena(K, THEME)
    view.packer = Packer()
    return view


def solo(game, **objects):
    """A session drawing only the given objects over the game's backdrop and shelters, on its tick and act. The
    boss, if any, comes with no parts, so enemies keep the pose they take beside a boss."""
    boss = None
    if game.boss is not None:
        boss = copy.copy(game.boss)
        boss.parts = []
    player = copy.copy(game.player)
    player.x = -100.0  # off the field: the ship is checked on its own
    members = dict(tick=game.tick, act=game.act, player=player, aliens=[], shots=[], pickups=[], ufo=None, boss=boss)
    members |= dict(bunkers=game.bunkers, hazards=[], effects=[])
    return SimpleNamespace(pilot=SimpleNamespace(game=SimpleNamespace(**members | objects)))


def drawn(view, base: bytes, current) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) in layer pixels, exclusive, of what differs from base, or None."""
    view.draw(PAINTER, current)
    px, w = view.layer.px, view.layer.w
    rows = [y for y in range(len(px) // w) if px[y * w : (y + 1) * w] != base[y * w : (y + 1) * w]]
    if not rows:
        return None
    columns = [x for y in rows for x in range(w) if px[y * w + x] != base[y * w + x]]
    return min(columns), rows[0], max(columns) + 1, rows[-1] + 1


def inside(box, x: float, y: float) -> bool:
    """Whether model position (x, y), as pixels, lies in the box, give or take the Frame's rounding."""
    left, top, right, bottom = box
    px, py = (x + 0.5) * K, (y + 0.5) * 2 * K
    return left - SLACK_X <= px <= right + SLACK_X and top - SLACK_Y <= py <= bottom + SLACK_Y


def covers(box, hazard) -> bool:
    """Whether the hazard's drawing (box) agrees with its Frame span (see the module's note)."""
    if box is None:
        return False
    left, top, right, bottom = box
    x0, x1 = hazard.x0 * K - SLACK_X, (hazard.x1 + 1) * K + SLACK_X
    if hazard.kind == "gap":
        return left <= x0 + K and right >= x1 - K  # its brackets stand just outside its cells
    if not (x0 <= left and right <= x1):
        return False
    if hazard.kind == "lane":
        return True
    return hazard.y0 * 2 * K - SLACK_Y <= top and bottom <= HEIGHT * 2 * K  # from the source row to the ground


def on_field(x: float, y: float) -> bool:
    return -0.5 <= x < WIDTH - 0.5 and -0.5 <= y < HEIGHT - 0.5


def mover(tick: int) -> tuple[str, ...]:
    return (("left",) if (tick // 90) % 2 else ("right",)) + (("fire",) if tick % 3 == 0 else ())


def check(game, view, seen: set, skipped: Counter) -> list[str]:
    """Every Frame entity of game's current tick against the arena's drawing; the failures."""
    frame, failures = observe(game), []
    view.draw(PAINTER, solo(game))
    base = bytes(view.layer.px)

    def against(label, current, x, y):
        if not on_field(x, y):
            skipped["centre off the field"] += 1
            return
        box = drawn(view, base, current)
        seen.add(label)
        if box is None or not inside(box, x, y):
            failures.append(f"tick {game.tick} {label} at ({x}, {y}): drawn at {box}")

    aliens = {alien.id: alien for alien in game.aliens}
    for enemy in frame.enemies:
        label = f"enemy {enemy.kind} {enemy.pose}" + (" veteran" if enemy.veteran else "") + (" cargo" * enemy.cargo)
        against(label, solo(game, aliens=[aliens[enemy.id]]), enemy.x, enemy.y)
    shots = {shot.id: shot for shot in game.shots}
    for shot in frame.shots:
        against(f"shot {shot.kind}", solo(game, shots=[shots[shot.id]]), shot.x, shot.y)
    pickups = {pickup.id: pickup for pickup in game.pickups}
    for pickup in frame.pickups:
        against(f"pickup {pickup.kind}", solo(game, pickups=[pickups[pickup.id]]), pickup.x, pickup.y)
    if frame.ufo is not None:
        against("ufo", solo(game, ufo=game.ufo), *frame.ufo)
    if frame.parts:
        parts = {part.id: part for part in game.boss.parts}
        for part in frame.parts:
            if part.state == "broken":
                skipped["broken part"] += 1
                continue
            boss = copy.copy(game.boss)
            boss.parts = [parts[part.id]]
            against(f"part {part.boss}.{part.name} {part.state}", solo(game, boss=boss), part.x, part.y)
    hazards = {hazard.id: hazard for hazard in game.hazards}
    for hazard in frame.hazards:
        label = f"hazard {hazard.kind} " + ("live" if hazard.live else "lock" if hazard.locked else "warn")
        box = drawn(view, base, solo(game, hazards=[hazards[hazard.id]]))
        seen.add(label)
        if not covers(box, hazard):
            failures.append(f"tick {game.tick} {label} {hazard[2:6]}: drawn at {box}")
    ship = frame.ship
    box = drawn(view, base, solo(game, player=game.player))
    if box is None and game.player.invulnerable and game.tick % FLICKER >= 7:
        skipped["ship flicker"] += 1
    else:
        seen.add("ship")
        if box is None or not inside(box, ship.x, ship.y):
            failures.append(f"tick {game.tick} ship at ({ship.x}, {ship.y}): drawn at {box}")
    view.draw(PAINTER, solo(game, bunkers={}))
    empty = bytes(view.layer.px)
    for x, y, _tier in frame.shelters[:: max(1, len(frame.shelters) // 8)]:  # a few cells of each shelter
        box = drawn(view, empty, solo(game, bunkers={(x, y): game.bunkers[(x, y)]}))
        seen.add("shelter")
        if box is None or not inside(box, x, y):
            failures.append(f"tick {game.tick} shelter cell ({x}, {y}): drawn at {box}")
    return failures


@pytest.fixture(scope="module")
def coverage():
    return {"seen": set(), "skipped": Counter(), "scenes": 0}


@pytest.mark.parametrize("scene", SCENES, ids=[f"{d}-w{w}-p{p}" for d, w, p in SCENES])
def test_every_frame_entity_lies_inside_what_the_arena_draws_for_it(scene, coverage):
    difficulty, wave, boss_phase = scene
    game = staged(Config(difficulty, "classic", wave), wave, boss_phase=boss_phase, phase="playing", immortal=True)
    view, failures = pixel_arena(), []
    for _ in range(TICKS):
        game.step(mover(game.tick))
        if game.tick % SAMPLE == 0:
            failures += check(game, view, coverage["seen"], coverage["skipped"])
    coverage["scenes"] += 1
    assert failures == [], failures[:10]


def test_the_scenes_cover_every_kind_of_entity(coverage):
    """Runs after the scenes: between them they drew every enemy kind and pose, every shot kind, boss part and
    hazard, and the rest."""
    if coverage["scenes"] < len(SCENES):
        pytest.skip("runs after every scene")
    seen = coverage["seen"]
    enemies = [label.split() for label in seen if label.startswith("enemy ")]
    assert {words[1] for words in enemies} == {
        *("scout", "diver", "gunner", "weaver", "shell", "lancer"),
        *("bomber", "splitter", "carrier", "mite", "guard"),
    }
    assert {words[2] for words in enemies} == {"entering", "formation", "telegraph", "diving", "returning"}
    assert any("veteran" in label for label in seen) and any("cargo" in label for label in seen)
    shots = {label.split()[1] for label in seen if label.startswith("shot ")}
    assert shots == {"player", "pellet", "dart", "hunter", "needle", "slug", "cluster"}
    parts = {label.split()[1] for label in seen if label.startswith("part ")}
    assert parts == {
        *("warden.core", "warden.turret", "hive_queen.queen", "dreadnought.core", "dreadnought.battery"),
        *("mothership.core", "mothership.pod"),
    }
    assert {"shielded", "open", "charge"} == {label.split()[2] for label in seen if label.startswith("part ")}
    assert {"hazard beam warn", "hazard beam lock", "hazard beam live", "hazard gap lock", "hazard lane lock"} <= seen
    assert {"ufo", "ship", "shelter"} <= seen and any(label.startswith("pickup ") for label in seen)


# --- Phase 5 spec 12.3: with the effects on, in each colour palette, the terminal shows exactly what was drawn ------

# (first tick, ticks drawn) of the classic tape: its first kills (129 on), their bursts and debris; and its first kick,
# the phase break born at 22646, which shifts the field sideways at ticks 22647-22652 (all of the arena's rows change)
ENCODED = {"kills": (100, 600), "kick": (22630, 50)}


def shows(vt, buf, theme) -> None:
    """tests/support/vt.py's assert_shows, the same verdict at a fraction of its cost: every character, and every
    cell's pen, where cells holding the same pen object with the same style are compared once, not cell by cell. On
    a difference assert_shows runs, to fail with its own message."""
    from tests.support.vt import assert_shows, expected_pen

    pens = {st: expected_pen(theme, st) for st in set(buf.styles)}
    shown = list(chain.from_iterable(vt.screen.pens))
    ids = list(map(id, shown))
    by_id, pairs = dict(zip(ids, shown)), set(zip(ids, buf.styles))
    same = vt.lines() == [buf.row_text(y) for y in range(buf.rows)] and len(shown) == len(buf.styles)
    if not (same and all(by_id[pen] == pens[st] for pen, st in pairs)):
        assert_shows(vt, buf, theme)
        raise AssertionError("the screen differs from the buffer")


def test_shows_fails_on_any_character_or_pen_the_screen_does_not_hold():
    """shows() gives assert_shows's verdict: the frame the screen holds passes both, and a buffer that differs from
    it by one cell's pen, or by one character, fails both."""
    from terminal_invaders.term.cells import CellBuffer
    from terminal_invaders.term.encode import Encoder
    from tests.support.vt import MiniVT, assert_shows

    theme = resolve_theme(SimpleNamespace(color="truecolor", theme="dark", paint_background=True), "default")
    styles = sorted(set(theme.styles().values()))
    buf = CellBuffer(12, 3)
    buf.addstr(0, 0, "ABCDEFGHIJKL", styles[1])
    buf.addstr(1, 2, "xy", styles[2])
    vt = MiniVT(12, 3)
    vt.feed(b"\x1b[?7l")
    vt.feed(Encoder(theme, SimpleNamespace(utf8=True)).encode(None, buf))
    for check in (shows, assert_shows):
        check(vt, buf, theme)
    for y, x, text, st in ((1, 3, "y", styles[3]), (0, 4, "Q", styles[1])):  # a pen, then a character
        other = buf.copy()
        other.addstr(y, x, text, st)
        for check in (shows, assert_shows):
            with pytest.raises(AssertionError):
                check(vt, other, theme)


@cache
def flown(first: int):
    """The classic tape's Session (effects `full`, palette default) stepped to tick `first`, once a process. A Session
    reads no palette, so a window in any palette starts from a copy, its palette set, as one made with it would be."""
    from terminal_invaders.cli import defaults
    from terminal_invaders.replay import Playback
    from terminal_invaders.ui import Session
    from tests.support import frames

    options = defaults(seed=1978, fixed_seed=True, quickstart=True, name="ACE", effects="full", palette="default")
    session = Session(options, frames.Profile(), frames.Audio(), Playback(frames.TAPE))
    game = session.pilot.game
    while game.tick < first:
        session.step((game.tick + 1) / 60)
    return session


@pytest.mark.parametrize("window", sorted(ENCODED))
@pytest.mark.parametrize("palette", ["default", "cvd", "contrast"])
def test_the_encoded_frame_shows_the_render_at_every_tick(palette, window, monkeypatch):
    """Every tick of a window, the Painter's frame (effects `full`, k = 2) encoded against the previous one and read
    back through MiniVT is the render, character and pen; the kick window holds the Painter's own kick (TL-12)."""
    from terminal_invaders import arena, fx
    from terminal_invaders.term.cells import CellBuffer
    from terminal_invaders.term.encode import Encoder
    from terminal_invaders.ui import Painter
    from tests.support.vt import MiniVT

    first, ticks = ENCODED[window]
    session = copy.deepcopy(flown(first))
    options = session.options
    options.palette = palette
    theme = resolve_theme(SimpleNamespace(color="truecolor", theme="dark", paint_background=True), palette)
    cols, rows = 120, 44
    buf = CellBuffer(cols, rows)
    painter = Painter(buf, options, theme)
    painter.limiter, painter.link = fx.FlashLimiter(), fx.Link()
    assert painter.layout.k == K
    kicks, kicking = [], arena.kicking
    monkeypatch.setattr(arena, "kicking", lambda *args: kicks.append(kicking(*args)) or kicks[-1])
    encoder, vt, previous = Encoder(theme, SimpleNamespace(utf8=True)), MiniVT(cols, rows), None
    vt.feed(b"\x1b[?7l")  # ENTER turns autowrap off
    game = session.pilot.game
    assert game.tick == first
    for _ in range(ticks):
        session.step((game.tick + 1) / 60)
        painter.draw(session, "")
        vt.feed(encoder.encode(previous, buf))
        shows(vt, buf, theme)
        previous = buf.copy()
    assert game.tick == first + ticks and game.kills > 0
    assert len(kicks) == ticks  # the Painter asked for the kick once a frame
    assert any(kicks) or window != "kick", "the kick window drifted off the kick"
