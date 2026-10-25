"""Sprite rules (Phase 4 spec 3.7, 15.4): every art key exists as a glyph and as px2 and px3 art, every sprite stays
inside its glyph's cells, the role rule holds, and the shape cues that carry the distinctions in 16 colours and mono
(per enemy kind and pose, per shot kind, per boss part frame, per hazard tile) hold at every scale."""

import math

import pytest

from terminal_invaders.arena import STARS
from terminal_invaders.model import HEIGHT, PICKUPS, WIDTH
from terminal_invaders.sprites import (
    ALIENS,
    CRACKED,
    GLYPHS,
    HAZARDS,
    PART_FRAMES,
    PARTS,
    PX2,
    PX3,
    REQUIRED,
    SHOTS,
    SPRITES2,
    SPRITES3,
    TELLS,
    artwork,
    backdrop,
    bounds,
    box,
    cell,
    doubled,
    frames,
    missing_art,
    missing_pickups,
    placed,
)
from terminal_invaders.sprites import PICKUPS as LETTERS
from terminal_invaders.term.pixels import PRIORITY, Art, PixelLayer, downsample
from terminal_invaders.theme import SLOT

SCALES = (2, 3, 4, 6)
KINDS = ("scout", "diver", "gunner", "weaver", "shell", "lancer", "bomber", "splitter", "carrier", "mite", "guard")
SHOT_KINDS = ("player", "pellet", "dart", "hunter", "needle", "slug", "cluster")
BOSS_PARTS = {  # spec 9: each part's width in cells
    "warden.core": 9,
    "warden.turret": 3,
    "hive_queen.queen": 11,
    "dreadnought.core": 9,
    "dreadnought.battery": 5,
    "mothership.core": 15,
    "mothership.pod": 5,
}


def rows_of(art) -> list[str]:
    return ["".join("#" if art.px[y * art.w + x] else "." for x in range(art.w)) for y in range(art.h)]


def shape(art) -> tuple:
    return art.w, art.h, bytes(bool(p) for p in art.px)


def pieces(art) -> int:
    """The art's groups of opaque pixels that touch, corners included."""
    opaque = {(i % art.w, i // art.w) for i, role in enumerate(art.px) if role}
    groups = 0
    while opaque:
        groups, todo = groups + 1, [opaque.pop()]
        while todo:
            x, y = todo.pop()
            for near in [(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]:
                if near in opaque:
                    opaque.remove(near)
                    todo.append(near)
    return groups


def test_every_art_key_of_the_spec_has_a_glyph_and_px2_and_px3_art():
    keys = (
        *KINDS,
        *(f"{kind}_tell" for kind in ("gunner", "lancer", "bomber", "splitter", "carrier")),
        *(f"{kind}_cracked" for kind in ("shell", "bomber", "splitter", "carrier", "guard")),  # the 2+ HP kinds
        "diving",
        *SHOT_KINDS,
        *(f"{part}{suffix}" for part in BOSS_PARTS for suffix in ("", "_charge", "_cracked")),
        *("beam_warn", "beam_live", "gap_mark", "lane_mark"),
    )
    assert set(keys) <= set(REQUIRED) and missing_art() == []
    assert missing_art(("armor", "boss", "bomb")) == ["armor", "boss", "bomb"]  # the retired keys are gone
    assert tuple(ALIENS) == KINDS and tuple(SHOTS) == SHOT_KINDS and set(PARTS) == set(BOSS_PARTS)
    assert missing_pickups() == [] and missing_pickups(("rapid", "laser")) == ["laser"]
    assert [LETTERS[kind] for kind in PICKUPS] == ["R", "S", "+", "T", "L", "B"]  # spec 3.7: R S + T L B


@pytest.mark.parametrize("k", SCALES)
def test_every_sprite_lies_inside_its_glyph_box(k):
    art = artwork(k)
    assert set(art) == set(GLYPHS)
    for name, sprites in art.items():
        for frame in sprites:
            assert frame.w <= len(GLYPHS[name]) * k and frame.h <= 2 * k, name
            for x, y in ((30, 10), (0, 0), (59, 19), (17, 3)):  # whole units: where the glyph arena draws it
                left, top, right, bottom = bounds(frame)
                px, py = placed(x, y, frame, k)
                outer = box(name, x, y, k)
                inside = outer[0] <= px + left and px + right <= outer[2] and outer[1] <= py + top
                assert inside and py + bottom <= outer[3], (name, k, (x, y), outer, (px, py))


def test_art_tables_cover_the_same_sprites_with_matching_frames():
    assert set(PX2) == set(PX3) and set(SPRITES2) == set(SPRITES3) == set(GLYPHS) - {"solid", "cracked", "star"}
    for name in PX2:
        assert PX2[name][0] == PX3[name][0], name  # the same role at every scale
        assert len(frames(PX2[name][1])) == len(frames(PX3[name][1])), name
    for kind, glyphs in ALIENS.items():
        assert len(frames(PX3[kind][1])) == len(glyphs) == 2  # two idle frames, like the glyphs
    for part, glyphs in PARTS.items():
        assert len(glyphs) == len(PART_FRAMES) and {len(glyph) for glyph in glyphs} == {BOSS_PARTS[part]}, part
    assert bounds(Art.from_rows([".#", ".."], 1)) == (1, 0, 2, 1) and bounds(Art.from_rows([".."], 1)) is None


@pytest.mark.parametrize("k", [4, 6])
def test_k4_and_k6_double_px2_and_px3(k):
    small, art = (SPRITES2 if k == 4 else SPRITES3), artwork(k)
    for name, (_, pictures) in small.items():
        assert [rows_of(frame) for frame in art[name]] == [doubled(rows) for rows in pictures], name


@pytest.mark.parametrize("k", SCALES)
def test_the_role_rule(k):
    """Every enemy kind, tell and cracked frame is role alien; only the dive poses are role diver (spec 3.7)."""
    art = artwork(k)

    def roles(name):
        return {px for frame in art[name] for px in frame.px} - {0}

    enemies = [*ALIENS, *(f"{kind}_tell" for kind in TELLS), *(f"{kind}_cracked" for kind in CRACKED)]
    assert all(roles(name) == {SLOT["alien"]} for name in enemies)
    assert roles("diving") == roles("mite_diving") == {SLOT["diver"]}
    assert roles("player") == {SLOT["player_shot"]} and roles("ship") == roles("shielded") == {SLOT["player"]}
    assert all(roles(kind) == {SLOT["enemy_shot"]} for kind in SHOT_KINDS[1:])
    assert all(roles(part + suffix) == {SLOT["boss"]} for part in PARTS for suffix in PART_FRAMES)
    assert roles("beam_live") == {SLOT["enemy_shot"]}  # a live beam is enemy fire; its marks are telegraphs
    assert all(roles(tile) == {SLOT["telegraph"]} for tile in HAZARDS if tile != "beam_live")


def test_glyphs_tell_every_enemy_kind_and_pose_apart():
    poses = [glyph for glyphs in ALIENS.values() for glyph in glyphs]
    poses += [*TELLS.values(), *CRACKED.values(), GLYPHS["diving"]]
    assert len(set(poses)) == len(poses)  # a glyph names one kind in one pose
    assert all(len(glyph) == 3 for glyph in poses if glyph not in ("w", "m"))
    for kind, tell in TELLS.items():
        assert tell not in ALIENS[kind] and len(tell) == len(ALIENS[kind][0])
    assert set(CRACKED) == {"shell", "bomber", "splitter", "carrier", "guard"}


@pytest.mark.parametrize("k", SCALES)
def test_silhouettes_tell_every_enemy_kind_and_pose_apart(k):
    art = artwork(k)
    names = [*ALIENS, *(f"{kind}_tell" for kind in TELLS), *(f"{kind}_cracked" for kind in CRACKED), "diving"]
    owners = {}
    for name in names:
        for frame in art[name]:
            assert owners.setdefault(shape(frame), name) == name, (k, name, owners[shape(frame)])
    for kind in TELLS:  # the tell is a third frame, unlike both idle frames
        assert shape(art[f"{kind}_tell"][0]) not in {shape(frame) for frame in art[kind]}, kind
    for kind in CRACKED:
        assert shape(art[f"{kind}_cracked"][0]) not in {shape(frame) for frame in art[kind]}, kind
    for formation, diving in zip(art["diver"], art["diving"], strict=True):
        assert shape(formation) != shape(diving)
    if k == 2:  # spec 15.4: silhouettes differ by kind at px2, 6 x 4
        assert {(art[kind][0].w, art[kind][0].h) for kind in ALIENS if kind != "mite"} == {(6, 4)}


@pytest.mark.parametrize("k", SCALES)
def test_shots_differ_in_shape(k):
    art, pixel = artwork(k), 2 if k in (4, 6) else 1  # an art pixel is 2 x 2 at the doubled scales
    shapes = {shape(frame) for kind in SHOT_KINDS for frame in art[kind]}
    assert len(shapes) == sum(len(art[kind]) for kind in SHOT_KINDS)  # no two kinds share a silhouette
    (player,) = art["player"]
    assert player.w == pixel and set(player.px) == {SLOT["player_shot"]}  # a solid line one art pixel wide
    (needle,) = art["needle"]
    assert needle.w == pixel and pieces(needle) == 2 and 0 in needle.px  # dashed: on, off, on
    first, second = art["pellet"]
    assert first.w == second.w == 2 * pixel and first.px != second.px  # two art pixels wide, in two phases
    for pellet in (first, second):
        columns = [{x for x in range(pellet.w) if pellet.px[y * pellet.w + x]} for y in range(pellet.h)]
        assert len({frozenset(column) for column in columns}) == 2  # it zig-zags between its two columns
    (dart,), (hunter,) = art["dart"], art["hunter"]
    assert pieces(dart) == 1 and pieces(hunter) == 2  # the hunter's arrowhead trails a dot
    (slug,) = art["slug"]
    assert slug.w >= 2 * pixel and 0 not in slug.px  # a solid block
    (cluster,) = art["cluster"]
    hollow = [(i % cluster.w, i // cluster.w) for i, role in enumerate(cluster.px) if not role]
    column = [[cluster.px[y * cluster.w + x] for y in range(cluster.h)] for x in range(cluster.w)]
    assert hollow and all(any(column[x][:y]) and any(column[x][y:]) for x, y in hollow)  # hollow inside


def test_shot_glyphs_differ():
    assert len(set(SHOTS.values())) == len(SHOTS) and SHOTS["player"] == "|"
    assert [SHOTS[kind] for kind in SHOT_KINDS[1:]] == ["!", "v", "V", ":", "%", "o"]  # spec 15.4


@pytest.mark.parametrize("k", SCALES)
def test_boss_part_frames_differ_and_the_shield_shuts(k):
    art = artwork(k)
    for part in PARTS:
        looks = [shape(art[part + suffix][0]) for suffix in PART_FRAMES]
        assert len(set(looks)) == len(PART_FRAMES), (part, k)
        open_, shielded = art[part][0], art[part + "_shielded"][0]
        assert sum(map(bool, shielded.px)) > sum(map(bool, open_.px))  # the window shuts
        assert open_.w <= BOSS_PARTS[part] * k
    for part, glyphs in PARTS.items():
        assert len(set(glyphs)) == len(glyphs), part


@pytest.mark.parametrize("k", SCALES)
def test_hazard_tiles_are_one_cell_and_differ(k):
    art = artwork(k)
    tiles = [art[tile][0] for tile in HAZARDS]
    assert all(tile.w <= k and tile.h == 2 * k for tile in tiles)
    assert len({shape(tile) for tile in tiles}) == len(tiles)
    (warn,), (beam,) = art["beam_warn"], art["beam_live"]
    pixel = 2 if k in (4, 6) else 1
    assert warn.w == pixel and 0 in warn.px  # a dotted 1-px column
    assert beam.w == k and 0 not in beam.px  # a solid band, cell-wide
    assert len(set(HAZARDS.values())) == len(HAZARDS)


@pytest.mark.parametrize("k", SCALES)
@pytest.mark.parametrize("act", [1, 2, 3, 4])
def test_backdrops_are_static_one_pixel_patterns_off_the_ground_row(act, k):
    pixels = set(backdrop(act, k, STARS))
    assert pixels and all(0 <= x < WIDTH * k and 0 <= y < 2 * (HEIGHT - 1) * k for x, y in pixels)
    for x, y in pixels:  # never a vertical line (the player shot) nor a zig-zag (the pellet)
        assert not {(x, y + 1), (x + 1, y + 1), (x - 1, y + 1)} & pixels, (act, k, x, y)
    if act == 1:  # the dots of Phase 3, one per star
        assert pixels == {(math.floor((x + 0.5) * k), (2 * y + 1) * k) for x, y in STARS}
    if act == 2:  # k-pixel dashes
        assert len(pixels) == k * len(STARS)
    if act == 3:  # dithered bands over rows 0-2 and 9-10
        assert {y // (2 * k) for _, y in pixels} == {0, 1, 2, 9, 10}
    if act == 4:  # a hull band in row 0
        assert {y // (2 * k) for _, y in pixels} == {0}


def test_backdrops_differ_by_act():
    assert len({frozenset(backdrop(act, 3, STARS)) for act in (1, 2, 3, 4)}) == 4
    with pytest.raises(ValueError, match="no backdrop for act 5"):
        backdrop(5, 3, STARS)


@pytest.mark.parametrize("k", SCALES)
def test_damaged_shelters_are_a_checkerboard_of_cells_that_survives_pooling(k):
    art = artwork(k)
    (solid,) = art["solid"]
    assert set(solid.px) == {SLOT["shelter"]} and (solid.w, solid.h) == (k, 2 * k)
    layer = PixelLayer(4 * k, 4 * k)
    for x in range(4):  # two rows of four cracked units, each in the phase the arena gives it
        for y in range(2):
            layer.blit(x * k, 2 * y * k, art["cracked"][(x + y) * k % 2])
    on = [[bool(layer.px[y * layer.w + x]) for x in range(layer.w)] for y in range(layer.h)]
    assert all(on[y][x] == ((x + y // 2) % 2 == 0) for y in range(layer.h) for x in range(layer.w))
    pooled = downsample(layer, 1, 2, PRIORITY)
    assert 0 in pooled.px and SLOT["shelter"] in pooled.px  # still dithered when half blocks are not available


@pytest.mark.parametrize("k", SCALES)
def test_pickup_rings_leave_their_centre_cell_for_the_letter(k):
    (ring,) = artwork(k)["pickup"]
    x, y = 30, 10
    px, py = placed(x, y, ring, k)
    col, row = int((x + 0.5) * k), int((y + 0.5) * k)  # the centre cell, which the letter overlay covers
    centre = {(col, 2 * row), (col, 2 * row + 1)}
    opaque = {(px + i % ring.w, py + i // ring.w) for i, role in enumerate(ring.px) if role}
    assert opaque and opaque - centre  # the ring shows around the letter
    if k % 3 == 0:  # px3 rings are hollow right where the letter goes
        assert not opaque & centre


def test_placement_is_smooth_and_centred():
    art = artwork(3)["ship"][0]
    lefts = [placed(30 + step * 0.4, 18, art, 3)[0] for step in range(6)]
    assert [b - a for a, b in zip(lefts, lefts[1:])] in ([1, 1, 2, 1, 1], [1, 2, 1, 1, 1])  # sub-cell steps
    assert placed(30, 18, art, 3) == (87, 108) and placed(-0.5, 0, art, 3)[0] == -4  # floor, not toward zero


def test_cells_take_halves_up_never_to_even():
    """Bug h: round() takes halves to even, so a descent by halves looked uneven at k = 1."""
    assert [cell(2 + 0.5 * n) for n in range(6)] == [2, 3, 3, 4, 4, 5]
    assert [round(2 + 0.5 * n) for n in range(6)] == [2, 2, 3, 4, 4, 4]  # what the glyph arena used to do
    assert (cell(-0.5), cell(-0.6), cell(0.49)) == (0, -1, 0)
