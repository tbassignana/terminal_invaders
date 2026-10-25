"""The arena (Phase 4 spec 15.1, 15.4) and the scaled block: every entity, pose, overlay, telegraph mark, hazard,
boss part and act backdrop drawn from hand-built model objects at k = 1 and in pixels; unknown kinds raising; bug h;
then the scale ladder, the HUD and footer in the block, the letterbox, motion rules, shelters, and the 2:1 pooled
fallback for terminals where half blocks are unsafe."""

import math
from types import SimpleNamespace

import pytest

from terminal_invaders import arena as arena_module
from terminal_invaders import fx
from terminal_invaders.app import Renderer
from terminal_invaders.arena import DIVE_KEYS, IDLE_TICKS, STARS, GlyphArena, PixelArena, bar_text
from terminal_invaders.model import HEIGHT, UFO, WIDTH, Alien, Boss, Effect, Hazard, Part, Pickup, Player, Shot
from terminal_invaders.sprites import (
    ALIENS,
    CRACKED,
    PARTS,
    RING,
    SHOTS,
    TELLS,
    artwork,
    backdrop,
    bounds,
    cell,
    placed,
)
from terminal_invaders.term.cells import A_BOLD, A_DIM, A_REVERSE, CellBuffer, style
from terminal_invaders.term.encode import Encoder
from terminal_invaders.term.pixels import LOWER, UPPER, Art
from terminal_invaders.theme import SLOT, resolve_theme
from terminal_invaders.ui import Painter
from tests.support.frames import STATS, options, session
from tests.support.terminals import capabilities, load
from tests.support.vt import MiniVT, assert_shows

SIXTEEN = resolve_theme(SimpleNamespace(color="16", theme="dark", paint_background=False))
MONO = resolve_theme(SimpleNamespace(color="mono", theme="dark", paint_background=False))
PAINTED = resolve_theme(SimpleNamespace(color="truecolor", theme="dark", paint_background=True))
AMBIGUOUS = SimpleNamespace(unicode_blocks=False)
SIZES = {1: (80, 24), 2: (120, 44), 3: (180, 64)}  # the smallest window of each scale: the block fills it
GROUND = HEIGHT - 1


def stage(**objects):
    """A session whose game holds only the given model objects (spec 3.2's arena members), built by hand."""
    members = dict(tick=0, act=1, player=Player(), aliens=[], shots=[], pickups=[], ufo=None, boss=None)
    game = SimpleNamespace(**members | dict(bunkers={}, hazards=[], effects=[]) | objects)
    return SimpleNamespace(pilot=SimpleNamespace(game=game))


def arena(current, k=1, theme=SIXTEEN, caps=None, painter=None, **settings):
    """The arena alone drawn on a block of scale k (a painter may be passed to draw on it again)."""
    if painter is None:
        painter = Painter(CellBuffer(*SIZES[k]), options(**settings), theme, caps)
        assert painter.layout.k == k
    painter.view.draw(painter, current)
    return painter


def at(painter, col: int, row: int, n: int = 1) -> tuple[str, set[int]]:
    """The text and styles of n cells from arena column col in arena row row (k = 1)."""
    buf = painter.screen
    start = (painter.top + 2 + row) * buf.cols + painter.left + col
    return "".join(buf.chars[start : start + n]), set(buf.styles[start : start + n])


def sprite(painter, x: float, y: float, n: int) -> tuple[str, set[int]]:
    """The cells a k = 1 sprite n glyphs wide covers at model position (x, y)."""
    return at(painter, cell(x) - n // 2, cell(y), n)


def ground(painter) -> str:
    return painter.screen.row_text(painter.top + 2 + GROUND)[painter.left : painter.left + WIDTH]


def slot(painter, role: str) -> int:
    return painter.colors[SLOT[role]]


def region(painter, x: float, y: float, art: Art) -> list[int]:
    """The layer's roles where art is placed at (x, y), pixel by pixel in art's order."""
    k, layer = painter.layout.k, painter.view.layer
    px, py = placed(x, y, art, k)
    return [layer.px[(py + i // art.w) * layer.w + px + i % art.w] for i in range(art.w * art.h)]


def roles_at(painter, x: float, y: float, name: str, frame: int = 0) -> set[int]:
    """The layer's roles under the opaque pixels of sprite name placed at (x, y)."""
    art = artwork(painter.layout.k)[name][frame]
    return {role for role, mine in zip(region(painter, x, y, art), art.px, strict=True) if mine}


def shows(painter, x: float, y: float, name: str, frame: int = 0, role: str | None = None) -> bool:
    """Whether the layer holds sprite name's opaque pixels at (x, y), in its own role or the role given."""
    art = artwork(painter.layout.k)[name][frame]
    want = SLOT[role] if role else None
    return all((got == (want or mine)) for got, mine in zip(region(painter, x, y, art), art.px, strict=True) if mine)


def telegraph_pixels(painter) -> set[tuple[int, int]]:
    layer = painter.view.layer
    return {(i % layer.w, i // layer.w) for i, role in enumerate(layer.px) if role == SLOT["telegraph"]}


def warden() -> Boss:
    parts = [
        Part(1, "core", 30, 3, 9, 172, 172, "shielded"),
        Part(2, "turret", 23, 4, 3, 20, 46, "open"),  # below half its HP: cracked
        Part(3, "turret", 37, 4, 3, 46, 46, "charge"),
    ]
    return Boss(30, 3, 238, 264, "warden", parts=parts, phases=2, state="fighting")


# ---- k = 1 ----


@pytest.mark.parametrize("theme", [PAINTED, SIXTEEN, MONO], ids=["truecolor", "16", "mono"])
def test_glyph_sprites_draw_in_their_roles_slots(theme):
    """Each glyph names its role, so every tier colours it from that role's row; the UFO, boss parts, pickups,
    live beams, the ship and veterans are bold, and the telegraph marks never are (spec 15.5)."""
    current = stage(
        aliens=[Alien(10, 5), Alien(20, 5, veteran=True), Alien(30, 5, "diver", diving=True, state="diving")],
        shots=[Shot(40, 8), Shot(50, 8, False, kind="dart")],
        pickups=[Pickup(10, 12, "bomb")],
        ufo=UFO(30, 1),
        effects=[Effect(40, 12, "+50")],
        bunkers={(5, 16): 3, (6, 16): 1},
        hazards=[Hazard(1, "beam", 14, 17, 10, 19, "lock")],
        boss=warden(),
    )
    player = current.pilot.game.player
    painter = arena(current, theme=theme)
    assert sprite(painter, 10, 5, 3) == (ALIENS["scout"][0], {slot(painter, "alien")})
    assert sprite(painter, 20, 5, 3) == (ALIENS["scout"][0], {slot(painter, "alien") | A_BOLD})  # a veteran
    assert sprite(painter, 30, 5, 3) == ("\\V/", {slot(painter, "diver")})
    assert sprite(painter, 40, 8, 1) == ("|", {slot(painter, "player_shot")})
    assert sprite(painter, 50, 8, 1) == ("v", {slot(painter, "enemy_shot")})
    assert sprite(painter, 10, 12, 3) == ("[B]", {slot(painter, "pickup") | A_BOLD})
    assert sprite(painter, 30, 1, 5) == ("=<=>=", {slot(painter, "ufo") | A_BOLD})
    assert sprite(painter, 30, 3, 9) == (PARTS["warden.core"][3], {slot(painter, "boss") | A_BOLD})  # shielded
    assert sprite(painter, 40, 12, 3) == ("+50", {slot(painter, "warn")})
    assert sprite(painter, 5, 16, 1) == ("#", {slot(painter, "shelter")})
    assert sprite(painter, 6, 16, 1) == (":", {slot(painter, "shelter")})
    assert at(painter, 14, GROUND, 3) == ("^^^", {slot(painter, "telegraph")})
    assert sprite(painter, player.x, player.y, 3) == ("/A\\", {slot(painter, "player") | A_BOLD})
    assert at(painter, *STARS[0]) == (".", {slot(painter, "star")})
    bar = painter.screen.row_text(painter.top + 2).index("WARDEN")
    assert painter.screen.styles[(painter.top + 2) * painter.screen.cols + bar] == slot(painter, "danger")
    assert not slot(painter, "telegraph") & A_BOLD or theme is MONO
    if theme is MONO:  # attributes only; telegraph marks are bold at k = 1
        attrs = {
            "player": A_BOLD,
            "shelter": A_DIM,
            "star": A_DIM,
            "pickup": A_REVERSE,
            "alien": 0,
            "telegraph": A_BOLD,
        }
        assert {role: slot(painter, role) for role in attrs} == attrs


def test_the_glyph_arena_rounds_halves_up_so_a_descent_is_even():
    """Bug h: k = 1 rows after n drops of 0.5 from y 2 are 2, 3, 3, 4, 4, ... (round() gave 2, 2, 3, 4, 4)."""
    rows = []
    for drop in range(6):
        painter = arena(stage(aliens=[Alien(30.5, 2 + 0.5 * drop)]))
        rows.append([row for row in range(HEIGHT) if ALIENS["scout"][0] in ground_row(painter, row)])
    assert rows == [[2], [3], [3], [4], [4], [5]]
    painter = arena(stage(aliens=[Alien(30.5, 4)]))
    assert ground_row(painter, 4).index(ALIENS["scout"][0]) == 30  # x 30.5 is cell 31, the glyph from 30


def ground_row(painter, row: int) -> str:
    return painter.screen.row_text(painter.top + 2 + row)[painter.left : painter.left + WIDTH]


@pytest.mark.parametrize("still", [False, True], ids=["moving", "reduced-motion"])
def test_glyph_poses(still):
    """Tells and cracked frames are state, shown under reduced motion; only the idle swap freezes. A diver or mite
    about to dive swaps its frames every 6 ticks in role diver, or shows its dive pose under reduced motion."""
    aliens = [
        Alien(5, 4, "gunner", state="telegraph"),
        Alien(15, 4, "shell", hp=1, max_hp=2),
        Alien(25, 4, "bomber", hp=1, max_hp=2, state="telegraph"),  # the tell wins over the crack
        Alien(35, 4, "diver", diving=True, state="diving"),
        Alien(45, 4, "diver", state="telegraph"),
        Alien(55, 4, "mite", state="telegraph"),
        Alien(5, 8, "scout", state="entering"),
        Alien(15, 8, "lancer", state="returning"),
        Alien(25, 8, "mite", diving=True, state="diving"),
    ]
    for tick in (0, 6, 20):
        painter = arena(stage(aliens=aliens, tick=tick), reduced_motion=still)
        idle, wobble = 0 if still else (tick // 20) % 2, (tick // 6) % 2
        alien, diver = {slot(painter, "alien")}, {slot(painter, "diver")}
        assert sprite(painter, 5, 4, 3) == ("(@)", alien)
        assert sprite(painter, 15, 4, 3) == ("[x]", alien)
        assert sprite(painter, 25, 4, 3) == ("{@}", alien)
        assert sprite(painter, 35, 4, 3) == ("\\V/", diver)
        assert sprite(painter, 45, 4, 3) == ("\\V/" if still else ALIENS["diver"][wobble], diver)
        assert sprite(painter, 55, 4, 1) == ("w" if still else ALIENS["mite"][wobble], diver)
        assert sprite(painter, 5, 8, 3) == (ALIENS["scout"][idle], alien)
        assert sprite(painter, 15, 8, 3) == (ALIENS["lancer"][idle], alien)
        assert sprite(painter, 25, 8, 1) == ("w", diver)


def test_a_diver_next_to_a_boss_wobbles_in_its_dive_pose():
    """Spec 9: the HIVE QUEEN's divers take the dive pose at once, so a boss and a diver never share a shape."""
    queen = Boss(30, 3, 132, 132, "hive_queen", parts=[Part(1, "queen", 30, 3, 11, 132, 132, "open")], phases=3)
    for tick in (0, 6):
        painter = arena(stage(aliens=[Alien(30, 7, "diver", state="telegraph")], boss=queen, tick=tick))
        assert sprite(painter, 30, 7, 3) == ("\\V/", {slot(painter, "diver")})


def test_veteran_and_cargo_overlays_at_k1():
    current = stage(
        aliens=[
            Alien(10, 5, "gunner", cargo=True),
            Alien(20, 5, "shell", veteran=True, cargo=True, hp=3, max_hp=3),
            Alien(30, 5, "mite", cargo=True, state="telegraph"),
            Alien(40, 5, "gunner", cargo=True, state="telegraph"),
        ]
    )
    painter = arena(current, reduced_motion=True)
    alien, pickup = slot(painter, "alien"), slot(painter, "pickup")
    assert sprite(painter, 10, 5, 3) == ("($)", {alien, pickup})
    assert at(painter, 10, 5) == ("$", {pickup})  # the middle glyph
    assert sprite(painter, 20, 5, 3) == ("[$]", {alien | A_BOLD, pickup | A_BOLD})
    assert at(painter, 30, 5) == ("w", {slot(painter, "diver")})  # a telegraph keeps its shape: no mark over it
    assert sprite(painter, 40, 5, 3) == ("(@)", {alien})


def test_beam_marks_and_the_live_beam_at_k1():
    """A beam's warning is `'` every other row from its source to the ground, `^^^` under it once its x locks,
    and three columns of bold `I` when live; nothing else on the ground row."""
    beam = Hazard(1, "beam", 29, 32, 4, 19, "warn", 40)
    current = stage(hazards=[beam], player=Player(x=5))
    painter = arena(current)
    marked = {row for row in range(HEIGHT) if at(painter, 30, row)[0] == "'"}
    assert marked == set(range(4, GROUND, 2)) and not ground(painter).strip()
    beside = [(col, row) for col in (29, 31) for row in range(HEIGHT) if (col, row) not in STARS]
    assert all(at(painter, col, row)[0] == " " for col, row in beside)  # one dotted column
    assert at(painter, 30, 18)[1] == {slot(painter, "telegraph")}
    beam.state = "lock"
    painter = arena(current)
    assert ground(painter).strip() == "^^^" and ground(painter).index("^") == 29
    beam.state = "live"
    painter = arena(current)
    assert all(at(painter, 29, row, 3) == ("III", {slot(painter, "enemy_shot") | A_BOLD}) for row in range(4, GROUND))
    assert "I" not in at(painter, 29, 3, 3)[0] and not ground(painter).strip()  # the ground row: marks only


def test_telegraph_marks_skip_shelter_cells():
    """A lane through a shelter shows above and below it, so the shelter still reads (spec 15.4)."""
    shelters = {(30, row): 3 for row in (14, 15, 16)} | {(30, GROUND): 3}
    current = stage(hazards=[Hazard(1, "beam", 29, 32, 4, 19, "lock")], bunkers=shelters, player=Player(x=5))
    painter = arena(current)
    assert [at(painter, 30, row)[0] for row in (12, 13, 14, 15, 16, 17, 18)] == ["'", " ", "#", "#", "#", " ", "'"]
    assert at(painter, 29, GROUND, 3)[0] == "^#^"
    pixels = arena(current, 3)
    k, inside = 3, set()
    for x, y in shelters:
        inside |= {(px, py) for px in range(x * k, (x + 1) * k) for py in range(2 * y * k, 2 * (y + 1) * k)}
    marks = telegraph_pixels(pixels)
    assert marks and not marks & inside
    assert {py // (2 * k) for _, py in marks} >= {12, 13, 17, 18, GROUND}  # above and below the shelter


def test_curtain_gaps_fill_in_and_safe_lanes_show_their_edges():
    gap = Hazard(1, "gap", 25, 32, 19, 19, "warn", 30)
    painter = arena(stage(hazards=[gap]))
    assert ground(painter)[19:38] == "- - -[       ]- - -"  # the gap is cells 25-31
    marked = [col for col in range(19, 38) if at(painter, col, GROUND)[0] != " "]
    assert {style for col in marked for style in at(painter, col, GROUND)[1]} == {slot(painter, "telegraph")}
    gap.ttl = 15
    assert ground(arena(stage(hazards=[gap])))[19:38] == "-----[       ]-----"
    painter = arena(stage(hazards=[Hazard(2, "lane", 20, 31, 4, 19, "warn", 30)]))
    assert ground(painter).strip() == "[         ]" and ground(painter).index("[") == 20


def test_boss_parts_by_state_and_the_boss_bar():
    painter = arena(stage(boss=warden()))
    boss = {slot(painter, "boss") | A_BOLD}
    assert sprite(painter, 30, 3, 9) == ("<=[===]=>", boss)  # shielded: the window shut
    assert sprite(painter, 23, 4, 3) == ("/t\\", boss)  # open, below half its HP: cracked
    assert sprite(painter, 37, 4, 3) == ("/@\\", boss)  # charging an attack
    line, text = painter.screen.row_text(painter.top + 2), "WARDEN  [########|##.]  I"
    assert line.index(text) == painter.left + (WIDTH - len(text)) // 2  # centred over arena row 0
    broken = warden()
    broken.parts[2].state = "broken"
    assert sprite(arena(stage(boss=broken)), 37, 4, 3)[0] == "   "  # broken: gone


def test_the_boss_bar_counts_down_with_a_mark_at_each_phase_threshold():
    """A mark sits where the HP still left when a phase begins ends the fill: the phases' HP from tables.BOSSES
    (WARDEN: 2 turrets x 46, then the core's 172), times the boss's parts open in them."""
    crews = {"warden": ["core", "turret", "turret"], "hive_queen": ["queen"], "mothership": ["core", *["pod"] * 4]}
    crews["dreadnought"] = ["battery", "core", "battery"]
    pools = {"warden": 264, "hive_queen": 308, "dreadnought": 340, "mothership": 325}

    def bar(kind, hp, phase=0):
        parts = [Part(index, name, 30, 3, 5, 1, 1) for index, name in enumerate(crews[kind])]
        return bar_text(Boss(30, 2, hp, pools[kind], kind, parts=parts, phases=4, phase=phase))

    assert bar("warden", 264) == "WARDEN  [########|###]  I"
    assert bar("warden", 172, 1) == "WARDEN  [########|...]  II"  # the turrets are gone: the fill meets the mark
    assert bar("mothership", 325) == "MOTHERSHIP  [##|####|####]  I"  # no mark for the escorts' phase at full HP
    assert bar("mothership", 1, 3) == "MOTHERSHIP  [#.|....|....]  IV"  # never empty while any HP is left
    assert bar("dreadnought", 0, 2) == "DREADNOUGHT  [...|....|...]  III"
    assert bar("hive_queen", 154, 1) == "HIVE QUEEN  [###|##.|....]  II"


def test_a_phase_break_shakes_the_parts_and_a_dying_boss_bursts():
    warden = Boss(30, 3, 46, 92, "warden", parts=[Part(1, "turret", 23, 4, 3, 46, 46, "shielded")], phases=2)
    warden.state = "break"
    looks = [sprite(arena(stage(boss=warden, tick=tick)), 23, 4, 3)[0] for tick in (0, 8, 16)]
    assert looks == ["{T}", "/t\\", "{T}"]
    assert sprite(arena(stage(boss=warden, tick=8), reduced_motion=True), 23, 4, 3)[0] == "{T}"
    warden.state = "dying"
    painter = arena(stage(boss=warden, tick=0))
    assert at(painter, 23, 4)[0] == "*" and sprite(painter, 23, 4, 3)[0] == "/*\\"
    assert sprite(arena(stage(boss=warden), reduced_motion=True), 23, 4, 3)[0] == "/t\\"


def test_the_bomb_ring_is_telegraph_dots_growing_from_the_ship():
    for ttl, radius in ((20, 5 / 3), (0, 5.0)):
        painter = arena(stage(effects=[Effect(30, 12, RING, ttl)]))
        dots = {(col, row) for row in range(HEIGHT) for col in range(WIDTH) if at(painter, col, row)[0] == "."}
        dots -= set(STARS)
        assert dots and all(abs(math.dist((col, row), (30, 12)) - radius) <= 1 for col, row in dots)
        assert {style for col, row in dots for style in at(painter, col, row)[1]} == {slot(painter, "telegraph")}
        assert "()" not in "".join(painter.screen.chars)  # never drawn as text
    still = arena(stage(effects=[Effect(30, 12, RING, 0)]), reduced_motion=True)  # an animation: not drawn
    assert slot(still, "telegraph") not in still.screen.styles


def test_a_shielded_or_guarded_ship():
    for player in (Player(shield=100), Player(guard=1), Player()):
        painter = arena(stage(player=player))
        glyph = "/A\\" if player == Player() else "{A}"
        assert sprite(painter, player.x, player.y, 3)[0] == glyph
        assert shows(arena(stage(player=player), 2), player.x, player.y, "ship" if glyph == "/A\\" else "shielded")


@pytest.mark.parametrize("k", [1, 2, 3])
def test_no_star_on_the_ground_row(k):
    assert len(STARS) == 29 and all(y != GROUND for _, y in STARS)
    painter = arena(stage(), k)
    if k == 1:
        assert not ground(painter).strip()
    else:
        layer = painter.view.layer
        assert not any(layer.px[2 * GROUND * k * layer.w :])


@pytest.mark.parametrize("k", [1, 2, 3])
@pytest.mark.parametrize(
    "objects",
    [
        dict(aliens=[Alien(10, 5, "armor")]),
        dict(aliens=[Alien(10, 5, state="asleep")]),
        dict(aliens=[Alien(10, 5, "scout", diving=True, state="diving")]),
        dict(shots=[Shot(10, 5, False, kind="bomb")]),
        dict(pickups=[Pickup(10, 5, "laser")]),
        dict(boss=Boss(30, 3, 10, 10, "kraken", parts=[Part(1, "core", 30, 3, 9, 10, 10)])),
        dict(boss=Boss(30, 3, 10, 10, "warden", parts=[Part(1, "wing", 30, 3, 9, 10, 10)])),
        dict(boss=Boss(30, 3, 10, 10, "warden", parts=[Part(1, "core", 30, 3, 9, 10, 10, "glowing")])),
        dict(boss=Boss(30, 3, 10, 10, "warden", parts=[Part(1, "core", 30, 3, 9, 10, 10)], state="sleeping")),
        dict(boss=Boss(30, 3, 10, 10, "kraken")),
        dict(hazards=[Hazard(1, "laser", 10, 13, 3, 19)]),
        dict(hazards=[Hazard(1, "beam", 10, 13, 3, 19, "armed")]),
        dict(hazards=[Hazard(1, "gap", 10, 17, 19, 19, "live")]),
        dict(hazards=[Hazard(1, "beam", 10, 10, 3, 19)]),
    ],
    ids=lambda objects: next(iter(objects)),
)
def test_unknown_kinds_raise_instead_of_drawing_a_stand_in(objects, k):
    with pytest.raises(ValueError, match="no art|unknown|covers no cell"):
        arena(stage(**objects), k)


# ---- pixels ----


def test_sprites_reach_the_layer_in_draw_order():
    current = stage(
        aliens=[Alien(20, 5), Alien(40, 5, "diver", diving=True, state="diving")],
        shots=[Shot(20, 5), Shot(30, 18, False, kind="slug")],
        pickups=[Pickup(10, 10, "shield")],
        ufo=UFO(50, 1),
        hazards=[Hazard(1, "beam", 9, 12, 2, 19, "lock")],  # its dotted column runs under the pickup
        boss=warden(),
    )
    painter = arena(current, 3)
    assert roles_at(painter, 20, 5, "player") == {SLOT["player_shot"]}  # shots over aliens
    assert roles_at(painter, 30, 18, "ship") == {SLOT["player"]}  # the ship over enemy fire
    assert roles_at(painter, 40, 5, "diving") == {SLOT["diver"]}
    assert roles_at(painter, 50, 1, "ufo") == {SLOT["ufo"]}
    assert roles_at(painter, 30, 3, "warden.core_shielded") == {SLOT["boss"]}
    assert roles_at(painter, 23, 4, "warden.turret_cracked") == {SLOT["boss"]}
    assert roles_at(painter, 37, 4, "warden.turret_charge") == {SLOT["boss"]}
    assert roles_at(painter, 10, 10, "pickup") == {SLOT["pickup"]}  # entities over telegraph marks
    assert roles_at(painter, 10, 16, "beam_warn") == {SLOT["telegraph"]}
    assert "WARDEN  [########|##.]  I" in painter.screen.row_text(painter.top + 2)
    roles = set(painter.view.layer.px)
    want = ("alien", "diver", "player_shot", "pickup", "ufo", "boss", "star", "telegraph", "player")
    assert {SLOT[name] for name in want} <= roles


@pytest.mark.parametrize("k", [2, 3])
def test_pixel_poses_and_the_dive_wobble(k):
    aliens = [
        Alien(10, 4, "gunner", state="telegraph"),
        Alien(20, 4, "carrier", hp=2, max_hp=3),
        Alien(30, 8, "diver", state="telegraph"),
        Alien(40, 8, "mite", diving=True, state="diving"),
    ]
    for tick, wobble in ((0, -0.25), (6, 0.25)):
        painter = arena(stage(aliens=aliens, tick=tick), k)
        assert shows(painter, 10, 4, "gunner_tell") and shows(painter, 20, 4, "carrier_cracked")
        assert shows(painter, 30 + wobble, 8, "diver", tick // 6, role="diver")  # shaking, in role diver
        assert shows(painter, 40, 8, "mite_diving")
    still = arena(stage(aliens=aliens, tick=6), k, reduced_motion=True)
    assert shows(still, 10, 4, "gunner_tell") and shows(still, 30, 8, "diving")  # tells are state


@pytest.mark.parametrize("k", [2, 3])
def test_veteran_plate_and_cargo_dot(k):
    current = stage(aliens=[Alien(10, 5, veteran=True), Alien(20, 5, cargo=True), Alien(30, 5)])
    painter, art = arena(current, k), artwork(k)["scout"][0]
    layer = painter.view.layer
    left, _, right, bottom = bounds(art)
    for x, plated in ((10, True), (30, False)):
        px, py = placed(x, 5, art, k)
        edge = layer.px[(py + bottom - 1) * layer.w + px + left : (py + bottom - 1) * layer.w + px + right]
        assert (set(edge) == {SLOT["alien"]}) is plated  # a 1-px plate along the bottom edge
    px, py = placed(20, 5, art, k)
    row = (py + art.h // 2) * layer.w + px + art.w // 2
    assert layer.px[row - 1 : row + 1] == bytes([SLOT["pickup"]] * 2)  # the gold mark: 2 pixels, ending at the centre
    assert sum(role == SLOT["pickup"] for role in layer.px) == arena_module.CARGO_PIXELS == 2


@pytest.mark.parametrize("k", [2, 3])
def test_one_cargo_pixel_gives_phase_4s_dot(k, monkeypatch):
    """CARGO_PIXELS = 1 (what tools/golden_identity.py sets) draws Phase 4's single centre dot (spec 3.7)."""
    monkeypatch.setattr(arena_module, "CARGO_PIXELS", 1)
    painter, art = arena(stage(aliens=[Alien(20, 5, cargo=True)]), k), artwork(k)["scout"][0]
    layer = painter.view.layer
    px, py = placed(20, 5, art, k)
    assert layer.px[(py + art.h // 2) * layer.w + px + art.w // 2] == SLOT["pickup"]
    assert sum(role == SLOT["pickup"] for role in layer.px) == 1


@pytest.mark.parametrize("k", [2, 3])
def test_a_cargo_enemy_shows_its_mark_in_mono_too(k):
    """Spec 15.4: shape first, then colour. Mono shows no roles, so the dot can't just paint a lit pixel gold: on
    every kind and idle frame the cargo enemy's cells differ from a plain one's (second review, SI4)."""
    for kind in ALIENS:
        for tick in (0, IDLE_TICKS):
            plain, cargo = (
                arena(stage(aliens=[Alien(20, 5, kind, cargo=marked)], tick=tick), k, MONO).screen
                for marked in (False, True)
            )
            assert (plain.chars, plain.styles) != (cargo.chars, cargo.styles), (kind, tick)


@pytest.mark.parametrize("k", [2, 3])
def test_the_veteran_plate_shows_on_every_enemy_frame(k):
    """Spec 7.1 and 15.4: a plated frame never looks like a plain frame of the same art (second review, SI6)."""
    view = PixelArena(k, MONO)
    keys = [*ALIENS, *(f"{kind}_tell" for kind in TELLS), *(f"{kind}_cracked" for kind in CRACKED)]
    for key in [*keys, *DIVE_KEYS.values()]:
        frames = range(len(view.art[key]))
        plain = [view.look(key, frame, "alien", False).px for frame in frames]
        for frame in frames:
            assert view.look(key, frame, "alien", True).px not in plain, (key, frame)


@pytest.mark.parametrize("k", [2, 3])
def test_pixel_beams_and_gaps(k):
    beam = Hazard(1, "beam", 29, 32, 4, 19, "warn")
    current = stage(hazards=[beam], player=Player(x=5))
    marks = telegraph_pixels(arena(current, k))
    centre = math.floor(30.5 * k)
    assert {x for x, _ in marks} == {centre} and {y // (2 * k) for _, y in marks} == set(range(4, GROUND))
    beam.state = "lock"
    marks = telegraph_pixels(arena(current, k))
    assert {x // k for x, y in marks if y // (2 * k) == GROUND} == {29, 30, 31}  # chevrons from the lock
    beam.state = "live"
    painter = arena(current, k)
    layer = painter.view.layer
    band = {layer.px[y * layer.w + x] for x in range(29 * k, 32 * k) for y in range(8 * k, 2 * GROUND * k)}
    assert band == {SLOT["enemy_shot"]} and not telegraph_pixels(painter)  # a solid 3-cell band
    gap = Hazard(2, "gap", 25, 32, 19, 19, "warn", 30)
    early = telegraph_pixels(arena(stage(hazards=[gap]), k))
    gap.ttl = 5
    late = telegraph_pixels(arena(stage(hazards=[gap]), k))
    assert early < late and {y // (2 * k) for _, y in late} == {GROUND}  # the dots fill in, on the ground row
    assert {x // k for x, _ in late} == set(range(19, 25)) | set(range(32, 38))  # brackets round the gap


@pytest.mark.parametrize("k", [2, 3])
@pytest.mark.parametrize("act", [1, 2, 3, 4])
def test_each_act_has_its_backdrop_and_mono_has_none(act, k):
    painter = arena(stage(act=act), k)
    layer = painter.view.layer
    stars = {(i % layer.w, i // layer.w) for i, role in enumerate(layer.px) if role == SLOT["star"]}
    assert stars == set(backdrop(act, k, STARS))
    assert SLOT["star"] not in arena(stage(act=act), k, MONO).view.layer.px


def test_the_backdrop_follows_the_act():
    current = stage()
    painter = arena(current, 2)
    first = bytes(painter.view.layer.px)
    current.pilot.game.act = 3
    assert bytes(arena(current, 2, painter=painter).view.layer.px) != first
    current.pilot.game.act = 1
    assert bytes(arena(current, 2, painter=painter).view.layer.px) == first


@pytest.mark.parametrize("k", [2, 3])
def test_the_bomb_ring_in_pixels(k):
    painter = arena(stage(effects=[Effect(30, 12, RING, 15)]), k)
    centre = ((30 + 0.5) * k, (12 + 0.5) * 2 * k)
    radii = {round(math.hypot((x - centre[0]) / k, (y - centre[1]) / (2 * k)), 1) for x, y in telegraph_pixels(painter)}
    assert radii and all(abs(r - 2.5) <= 0.5 for r in radii)
    assert not telegraph_pixels(arena(stage(effects=[Effect(30, 12, RING, 15)]), k, reduced_motion=True))


@pytest.mark.parametrize("theme, letter", [(SIXTEEN, SIXTEEN.style("pickup")), (MONO, A_REVERSE)])
def test_pickup_letters_and_popups_are_text_over_the_pixels(theme, letter):
    current = stage(pickups=[Pickup(10, 10, "bomb")], effects=[Effect(59, 12, "+1000")])
    painter = arena(current, 3, theme)
    top, left, buf = painter.top + 2, painter.left, painter.screen
    centre = (top + 31) * 180 + left + 31  # floor((10 + 0.5) * 3): the cell under the ring's centre
    assert (buf.chars[centre], buf.styles[centre]) == ("B", letter)
    popup = buf.row_text(top + 37)  # floor((12 + 0.5) * 3)
    assert popup.rstrip().endswith(" +100") and len(popup.rstrip()) == left + 180  # clipped at the arena's edge


def reduced_motion_layers(still, **settings):
    """The k = 3 layer and whether the +50 popup shows, at ticks 0, 4 and 20, with motion reduced or not."""
    current = stage(aliens=[Alien(20, 5)], shots=[Shot(30, 10, False, kind="pellet")], effects=[Effect(40, 8, "+50")])
    out = []
    for tick in (0, 4, 20):
        current.pilot.game.tick = tick
        painter = arena(current, 3, reduced_motion=still, **settings)
        out.append((bytes(painter.view.layer.px), "+50" in "".join(painter.screen.chars)))
    return out


@pytest.mark.parametrize("effects", ["off", "full"])
def test_reduced_motion_freezes_the_animation_and_draws_popups_still(effects):
    """Spec 2.8: reduced motion freezes the animation and draws the effect texts still, where Phase 4 hid them; the
    level is off, so `full` adds nothing to the frozen layer."""
    moving, still = reduced_motion_layers(False), reduced_motion_layers(True, effects=effects)
    assert len({layer for layer, _ in moving}) == 3 and all(shown for _, shown in moving)
    assert len({layer for layer, _ in still}) == 1 and all(shown for _, shown in still)
    assert SLOT["spark"] not in still[0][0]


def test_still_texts_off_gives_phase_4s_hidden_popups(monkeypatch):
    monkeypatch.setattr(arena_module, "STILL_TEXTS", False)  # what tools/golden_identity.py sets
    still = reduced_motion_layers(True)
    assert len({layer for layer, _ in still}) == 1 and not any(shown for _, shown in still)
    glyphs = arena(stage(effects=[Effect(40, 8, "+50")]), 1, reduced_motion=True)
    assert "+50" not in "".join(glyphs.screen.chars)


def test_reduced_motion_draws_the_k1_texts_still_but_not_the_ring():
    ringed = stage(effects=[Effect(40, 8, "+50", 10), Effect(20, 12, RING, 15)])
    painter = arena(ringed, 1, reduced_motion=True, effects="full")
    assert at(painter, 39, 8, 3)[0] == "+50"  # in its place: the level is off, so no rise
    plain = arena(stage(effects=[Effect(40, 8, "+50", 10)]), 1, reduced_motion=True, effects="full")
    assert (painter.screen.chars, painter.screen.styles) == (plain.screen.chars, plain.screen.styles)  # no ring


def test_an_invulnerable_player_flickers_unless_motion_is_reduced():
    current = stage(player=Player(invulnerable=30))
    game = current.pilot.game
    seen = []
    for tick in (6, 7, 11, 12):
        game.tick = tick
        seen.append(roles_at(arena(current, 3), game.player.x, game.player.y, "ship") == {SLOT["player"]})
    assert seen == [True, False, False, True]
    game.tick = 7
    shown = arena(current, 3, reduced_motion=True)
    assert roles_at(shown, game.player.x, game.player.y, "ship") == {SLOT["player"]}
    assert sprite(arena(current), game.player.x, game.player.y, 3)[0] == "   "  # k = 1 flickers too


def test_shelters_follow_their_damage():
    current = stage(bunkers={(10, 14): 3, (11, 14): 3})
    painter = None

    def shelter_pixels():
        nonlocal painter
        painter = arena(current, 3, painter=painter)
        return sum(role == SLOT["shelter"] for role in painter.view.layer.px)

    checker = sum((x + y // 2) % 2 == 0 for x in range(30, 33) for y in range(84, 90))  # unit (10, 14) at k = 3
    assert shelter_pixels() == 2 * 3 * 6
    current.pilot.game.bunkers[(10, 14)] = 1
    assert shelter_pixels() == 3 * 6 + checker == 28
    del current.pilot.game.bunkers[(11, 14)]
    assert shelter_pixels() == checker


def test_ambiguous_wide_terminals_get_the_pixel_arena_pooled_into_spaces():
    painter = arena(stage(aliens=[Alien(20, 5)], shots=[Shot(30, 10)]), 3, caps=AMBIGUOUS)
    buf = painter.screen
    assert painter.view.packer.pooled and not {UPPER, LOWER} & set(buf.chars)
    shot = (painter.top + 2 + 31) * 180 + painter.left + 91  # the shot's cell: pooled, a space in its colour
    assert (buf.chars[shot], buf.styles[shot]) == (" ", style(bg=SLOT["player_shot"]))
    vt = MiniVT(180, 64)
    vt.feed(b"\x1b[?7l" + Encoder(SIXTEEN, SimpleNamespace(utf8=False)).encode(None, buf))  # ASCII only
    assert_shows(vt, buf, SIXTEEN)


def test_the_mono_pixel_arena_uses_attributes_only():
    painter = arena(
        stage(aliens=[Alien(20, 5)], bunkers={(10, 14): 3}, hazards=[Hazard(1, "lane", 20, 31, 4, 19)]), 3, MONO
    )
    styles = painter.screen.styles
    assert not [st for st in styles if st & 0xFFFF] and A_REVERSE in {st & A_REVERSE for st in styles}
    assert SLOT["telegraph"] in painter.view.layer.px  # marks are shapes, drawn in mono too


@pytest.mark.parametrize("k", [2, 3])
def test_every_shot_kind_draws_its_own_art(k):
    shots = [Shot(5 + 7 * i, 10, kind == "player", kind=kind) for i, kind in enumerate(SHOTS)]
    for tick in (0, 4):
        painter = arena(stage(shots=shots, tick=tick), k)
        for shot in shots:
            frame = (tick // 4) % len(artwork(k)[shot.kind])
            assert shows(painter, shot.x, shot.y, shot.kind, frame), (shot.kind, tick)


# ---- the block (through the Session and Painter) ----


def field(**objects):
    """A session in play with only the given objects in its arena (no shelters, no aliens unless given)."""
    current = session()
    game = current.pilot.game
    game.aliens, game.shots, game.pickups, game.effects, game.bunkers, game.hazards = [], [], [], [], {}, []
    game.ufo = game.boss = None
    game.act, game.phase = 1, "playing"  # past the wave banner
    for name, value in objects.items():
        setattr(game, name, value)
    return current


def drawn(current, cols=180, rows=64, theme=SIXTEEN, caps=None, **settings):
    for name, value in settings.items():
        setattr(current.options, name, value)
    painter = Painter(CellBuffer(cols, rows), current.options, theme, caps)
    painter.draw(current)
    return painter


@pytest.mark.parametrize(
    "cols, rows, scale, k",
    [(80, 24, "auto", 1), (119, 44, "auto", 1), (120, 44, "auto", 2), (240, 70, "auto", 3), (240, 70, "2", 2)]
    + [(240, 70, "1", 1), (359, 124, "auto", 4), (360, 124, "auto", 6), (360, 124, "3", 3), (100, 30, "6", 1)],
)
def test_the_arena_follows_the_ladder_and_the_scale_flag(cols, rows, scale, k):
    painter = drawn(field(), cols, rows, scale=scale)
    assert painter.layout.k == k
    assert isinstance(painter.view, GlyphArena if k == 1 else PixelArena)
    assert (painter.width, painter.height) == (60 * k, 20 * k + 4)


def test_the_hud_and_footer_keep_their_proportions_and_panels_centre_in_the_block():
    small = drawn(field(), 80, 24)
    current = field()
    current.pause("manual")
    painter = drawn(current, 240, 70, show_fps=True)
    painter.draw(current, STATS.format(k=3, tier="16"))
    top, left, buf = painter.top, painter.left, painter.screen
    assert (top, left, painter.k) == (3, 30, 3)
    for row, col in ((0, 39), (1, 36), (1, 44)):  # spec 15.2's WAVE, GUN and BOMB fields: columns x k
        word = small.screen.row_text(small.top + row)[small.left + col :].split(" ")[0]
        assert word and buf.row_text(top + row)[left + 3 * col :].startswith(word), (row, col, word)
    assert buf.row_text(top + 63)[left : left + 180].rstrip() == STATS.format(k=3, tier="16")  # the block's last row
    assert buf.row_text(top + 62).startswith(" " * left + "Tap A/D")
    paused = next(y for y in range(70) if "PAUSED" in buf.row_text(y))
    line = buf.row_text(paused)
    assert line.index("PAUSED") + 3 == left + 90 and 26 <= paused - top <= 34  # centred in the 180 x 64 block


def test_menus_draw_as_a_card_in_the_middle_of_the_block():
    current = session(quickstart=False)
    painter = drawn(current, 240, 70)
    assert (painter.top, painter.left, painter.width, painter.height, painter.k) == (23, 90, 60, 24, 1)
    assert painter.screen.row_text(23 + 12)[90 + 13 : 90 + 21] == "> LAUNCH"


def test_the_letterbox_is_painted_only_with_a_painted_background():
    painter = drawn(field(), 240, 70, theme=PAINTED)
    styles, edge = painter.screen.styles, style(bg=SLOT["letterbox"])
    outside = [(y, x) for y in range(70) for x in range(240) if not (3 <= y < 67 and 30 <= x < 210)]
    assert all(styles[y * 240 + x] == edge for y, x in outside)
    assert edge not in styles[3 * 240 + 30 : 3 * 240 + 210]
    menu = drawn(session(quickstart=False), 240, 70, theme=PAINTED)  # the card's surroundings, inside the block too
    assert menu.screen.styles[10 * 240 + 100] == edge and menu.screen.styles[30 * 240 + 100] != edge
    assert edge not in drawn(field(), 240, 70).screen.styles


@pytest.mark.parametrize("fixture, pooled", [("ghostty", False), ("linux-console", False), ("iterm2-3.4", True)])
def test_the_renderer_passes_the_terminal_to_the_arena(fixture, pooled):
    renderer = Renderer(options(show_fps=True), capabilities(load(fixture)), (240, 70))
    current = field()
    data = renderer.frame(current, 0.0, True, SimpleNamespace(srtt=None, mode="off", disabled=False, reserved=set))
    assert renderer.painter.view.packer.pooled is pooled
    assert f" k3 {renderer.theme.tier}" in renderer.buffer.row_text(66)
    assert (UPPER.encode() in data or LOWER.encode() in data) is not pooled  # the stars


# ---- Phase 5's effect hooks (spec 2.10) ----


def kill(x: float, y: float, age: int, text: str = "+10") -> Effect:
    """A kill popup (spec 2.2) first seen `age` ticks ago."""
    ttl0 = fx.recognise(Effect(x, y, text))[1]
    return Effect(x, y, text, ttl0 - age)


def both(current, k: int, theme=SIXTEEN, **settings):
    """The arena at level full with the kick on, and drawn again with the kick held off (a remote link, spec 2.8)."""
    on = arena(current, k, theme, effects="full", **settings)
    held = Painter(CellBuffer(*SIZES[k]), options(effects="full", **settings), theme, None)
    held.link = fx.Link(remote=True)
    return on, arena(current, k, painter=held)


@pytest.mark.parametrize("k", [2, 3])
def test_a_shot_and_the_ship_draw_over_debris(k):
    effect = kill(30, 10, 9)
    points = fx.debris(stage(effects=[effect]).pilot.game, k, fx.FULL)
    (x, y, role) = points[0]
    assert role == "spark"
    shot = arena(stage(effects=[effect], shots=[Shot(x, y)]), k, effects="full")
    assert roles_at(shot, x, y, "player") == {SLOT["player_shot"]} and SLOT["spark"] in shot.view.layer.px
    ship = arena(stage(effects=[effect], player=Player(x=x, y=y)), k, effects="full")
    assert roles_at(ship, x, y, "ship") == {SLOT["player"]} and SLOT["spark"] in ship.view.layer.px


@pytest.mark.parametrize("k", [2, 3])
def test_marks_and_shelters_draw_over_spark(k):
    """Debris and bursts take background and backdrop pixels only: a telegraph mark and a shelter keep theirs."""
    hazards = [Hazard(1, "beam", 29, 32, 4, 19, "warn")]  # its dotted column runs through the kills below
    bunkers = {(28, 10): 3, (32, 11): 3}
    effects = [kill(30, 10, age) for age in (2, 6, 10)] + [kill(30, 12, 12, "+1,500")]  # its kick is over
    plain = arena(stage(hazards=hazards, bunkers=bunkers), k, effects="full")
    busy = arena(stage(hazards=hazards, bunkers=bunkers, effects=effects), k, effects="full")
    before, after = plain.view.layer.px, busy.view.layer.px
    kept = [i for i, role in enumerate(before) if role in (SLOT["telegraph"], SLOT["shelter"])]
    assert kept and all(after[i] == before[i] for i in kept)
    changed = {after[i] for i in range(len(after)) if after[i] != before[i]}
    assert SLOT["spark"] in changed <= {SLOT["spark"], SLOT["star"]}  # debris, fading to star, on background only


def test_k1_bursts_never_cover_a_mark_or_a_shelter():
    hazards = [Hazard(1, "lane", 20, 31, 4, 19)]
    current = stage(hazards=hazards, bunkers={(40, 12): 3})
    tiles = arena_module.marks(hazards, {}, 2)
    tx, ty, _ = tiles[len(tiles) // 2]
    plain = arena(current, 1, effects="full")
    current.pilot.game.effects = [kill(tx, ty, 2), kill(40, 12, 2), kill(50, 6, 2)]  # frame 0: "&" at the centre
    busy = arena(current, 1, effects="full")
    assert at(busy, tx, ty) == at(plain, tx, ty) and at(busy, 40, 12) == at(plain, 40, 12)
    assert at(busy, 50, 6)[0] == "&" and slot(busy, "spark") in at(busy, 50, 6)[1]


def kick_scene() -> SimpleNamespace:
    boss = warden()
    boss.state = "dying"  # its parts burst: the `*` overlay
    effects = [kill(30, 8, 3, "+5,000"), kill(45, 12, 3)]
    return stage(boss=boss, pickups=[Pickup(10, 10, "bomb")], aliens=[Alien(20, 5)], effects=effects, tick=101)


def test_the_kick_shifts_the_pixel_layer_and_the_text_over_it():
    current = kick_scene()
    dx = fx.kick(current.pilot.game, fx.FULL, None)
    assert dx in (-1, 1)
    on, held = both(current, 3)
    layer = held.view.layer
    assert on.view.layer.px == fx.shift(layer.px, layer.w, dx) != layer.px
    top, left = on.top + 2, on.left

    def find(painter, text, row):
        line = painter.screen.row_text(top + row)
        return line.index(text, left)

    assert find(on, "B", 31) == find(held, "B", 31) + dx  # the pickup letter
    assert find(on, "+5,000", 25) == find(held, "+5,000", 25) + dx  # the popup
    part = arena_module.burst(current.pilot.game.boss, 101)
    row = math.floor((part.y + 0.5) * 3)
    assert find(on, "*", row) == find(held, "*", row) + dx  # the boss's bursting part
    assert find(on, "WARDEN", 0) == find(held, "WARDEN", 0)  # the boss bar stays
    for row in (on.top, on.top + 1, on.top + 2 + 3 * HEIGHT):  # the HUD and the footer stay
        assert on.screen.row_text(row) == held.screen.row_text(row)


def test_the_kick_moves_the_whole_k1_field_but_the_boss_bar():
    current = kick_scene()
    dx = fx.kick(current.pilot.game, fx.FULL, None)
    on, held = both(current, 1)
    left = on.left
    for row in range(1, HEIGHT):
        moved = on.screen.row_text(on.top + 2 + row)[left : left + WIDTH]
        still = held.screen.row_text(held.top + 2 + row)[left : left + WIDTH]
        assert moved == (" " + still[:-1] if dx > 0 else still[1:] + " "), row
    bar = on.screen.row_text(on.top + 2)
    assert bar.index("WARDEN") == held.screen.row_text(held.top + 2).index("WARDEN")
    for row in (on.top, on.top + 1):
        assert on.screen.row_text(row) == held.screen.row_text(row)


@pytest.mark.parametrize("k", [1, 2, 3])
def test_no_kick_outside_play_or_under_a_limiter_that_refuses(k):
    current = kick_scene()
    _, held = both(current, k)
    for scene in (dict(scene="paused"), dict(countdown_at=3.0)):
        moment = SimpleNamespace(**vars(current) | scene)
        painter = arena(moment, k, effects="full")
        assert painter.screen.chars == arena(moment, k, painter=held).screen.chars, scene
    current.pilot.game.phase = "wave_clear"
    assert arena(current, k, effects="full").screen.chars == arena(current, k, painter=held).screen.chars
    current.pilot.game.phase = "playing"
    refusing = Painter(CellBuffer(*SIZES[k]), options(effects="full"), SIXTEEN, None)
    refusing.limiter = fx.FlashLimiter(limit=0)
    assert arena(current, k, painter=refusing).screen.chars == arena(current, k, painter=held).screen.chars


@pytest.mark.parametrize("k", [1, 2, 3])
def test_level_off_draws_no_spark_and_phase_4s_stars(k):
    current = stage(effects=[kill(30, 10, 6), kill(30, 8, 3, "+5,000")], tick=600)
    off = arena(current, k)
    full = arena(current, k, effects="full")
    assert full.screen.chars != off.screen.chars
    if k == 1:
        assert not {"&", ";"} & set(off.screen.chars) and "&" in full.screen.chars  # (the popup has a comma)
        assert at(off, 0, 0, WIDTH) == at(arena(stage(), 1), 0, 0, WIDTH)  # Phase 4's fixed stars
    else:
        assert SLOT["spark"] not in off.view.layer.px and SLOT["spark"] in full.view.layer.px
        assert off.view.layer.px == arena(stage(tick=600), k).view.layer.px  # the backdrop alone: Phase 4's
