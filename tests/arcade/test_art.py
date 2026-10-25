"""Arcade art (spec 5): our bitmaps sit at the ROM's addresses with the ROM's sizes and silhouettes, differ from the
ROM's pictures except the exempt ones (EXEMPT: the shield and the solid shots, whose extents fix every pixel, and the
pictures whose every lit pixel decides play: the patterned alien shots, both shot explosions and the cannon's two
blow-ups), and the font covers and spells every message the game part prints. The ROM is never read here: its
bitmaps are known by their extents (art.EXTENTS) and sha256 digests (ROM_DIGESTS)."""

import hashlib

import pytest

from terminal_invaders.arcade import art
from terminal_invaders.arcade.image import build_image
from terminal_invaders.arcade.tables import TABLES

# sha256 of each ROM bitmap, so the tests can tell ours apart without holding the ROM's bytes.
ROM_DIGESTS = {
    "alien_a0": "42f557b0750a7618dac05b8e512ac668252f660a58c194061de72cda30c6a92a",
    "alien_b0": "8813a44aa4e5f4d3fb3dc0f1fb23ff399644e6cdba7495c9d04be5c1b63a9857",
    "alien_c0": "98a25d23c61e2f3000af7b76f4db61e31129b9b0394f40115023e8ac47635d8e",
    "alien_a1": "437e5c2b568dfa30bf4d497e667c4cb64f83a6cbc6839535f394508a9ea1f936",
    "alien_b1": "3ecd17fd592181922d0f375466846f435e01bf1371ef7a387df8f872b3eb4dbb",
    "alien_c1": "1ba743418eb88a7645487469ed7e5473402d1856bfea47a38e229db42d9ddca1",
    "player": "a0faab8cce313862c6b79baa6c118f37b09768e2d6264eb52931f130cd60cd2d",
    "player_blowup0": "2e1bb3d29b454cdd0bd7ad08079d34abe9a4e05ce57788ca0ef54e64c1dbdb28",
    "player_blowup1": "a4f9dc105569e0740d7d7fc028f34e7e7688fc22c41eec19e96d0864b5107ef5",
    "player_shot": "dc0e9c3658a1a3ed1ec94274d8b19925c93e1abb7ddba294923ad9bde30f8cb8",
    "shot_explosion": "4e2465e93515ebee243577f422f8a2c26c9ee1277c031b5af964bc1ca578785e",
    "alien_explosion": "da04cc0e2a3f3e5ce9017d10011e4cc79c2011dcaa0052ac96f66b06cb877a99",
    "squiggly0": "77eb57071662b68a76d5dfdaeaf48d3a395050565f53a6b133d3d9f17e1d42c7",
    "squiggly1": "73b5cec42b370ac5ccca4e26d80123c52547033fe7766b9b21bd82b36b04783b",
    "squiggly2": "c382963324cd2564609d1ccaa6f2fe750c5c31000dd869fb65df057684ab997c",
    "squiggly3": "1e40e0d6e7d36edbd3519eaf3aa7661e66a4bfd191ca9437fad7b3421f41b132",
    "alien_shot_explosion": "e8d8def1728d6e6dbfcc9ed4ac6a0f11cf80a020168d9aa735ce06a6234ce31a",
    "plunger0": "61bac9a47e4e74deefc7daa57c3e4057890150ed8b189a33209144705a597048",
    "plunger1": "7e0fd8ec286bf37f0102475cd3ce4a5dc4463b65eafc1997cfde61fb391370b3",
    "plunger2": "80942b005ceafbe99ab122cab079277dd81cc25823776ca2a5a53981dad39fbf",
    "plunger3": "0da6ae7f4b3b757359201a26e14ab474c890f9b35c47ecacf15de9e68bee3140",
    "rolling0": "ea5cb94fd022d7e5d54cccc059ce72c542aa450386d04e179fdccc12028f55fc",
    "rolling1": "290cea48868091531453b6384f557222d36624926490c3af3ae62340350e332b",
    "rolling2": "ea5cb94fd022d7e5d54cccc059ce72c542aa450386d04e179fdccc12028f55fc",
    "rolling3": "63614323c43d4ab1ec4bcc9c4a3495073758a85225610ebb80ef26f41ee729bc",
    "shield": "abde57e77f62bfa5d92a137121a51e81d07b68bf947a49416a2d1cdbb8b292c5",
    "saucer": "e6158b34b729884b46ec900931337dbdf290626fd8a775e818dff32ad1a2c45c",
    "saucer_explosion": "87d09370d2dc6743396ff138eb915fd702be67dbf8e098cc3d380ba7ffaeb02e",
    "glyph_00": "2f6e00b56422d73fd6b3ce32e678c065e31d57f16425ee22be35fca31900a6c2",
    "glyph_01": "2a0a12608a7ce97ec1d8b2df572903a0a9214d3f4ff22c8ccbe2935cfaffa6b7",
    "glyph_02": "095dc9caf9bc748319e0d773869a4360a08922d5c7b9e6caf6f58025beefef29",
    "glyph_03": "4cf54b38ab60871f78ca489677cab2614d852f0cfccc4c507ba9d8aff7595eb4",
    "glyph_04": "e4edd361595f9630f20127b4dc59773a047f2c20eb539c447b4395b347d63eae",
    "glyph_05": "21aaa14d60cb58169a0849e74ba88a33b691e9b4f1a773dc8dd192de5cb530a0",
    "glyph_06": "acdd71669f175a6f362da415ef0b05534f432d85963d42017cc4652f8be27c9e",
    "glyph_07": "ea509eca0e979ed2d916a8fb29dd7976bd69c0f3511d2794969ad748d99ad9bf",
    "glyph_08": "acf6c54801de4fffa644e2244c7e6a755006bf569c2d34119d60e1aa5f489e4d",
    "glyph_09": "86c1b8ca92e4927c66effc2866dde360e43e888344901b0886942a6a6c079d57",
    "glyph_0A": "4971f6d4e8f04f1633c06883b79feddb8ab3176178c1d0a04c57de57edde3cd5",
    "glyph_0B": "0f661832b81395a7f59f296185c9200073209a6a33e84c079616a30ad9a1802a",
    "glyph_0C": "c4546b4b8baec41bcb0f3684328381bbc842a0fab527796a43a47ada22cbbe5e",
    "glyph_0D": "3ffdd1beaab6b64fe69227b94525aa2c54f4a7229f7dbbbac66eafde40b128fc",
    "glyph_0E": "7a43c63bfdee7095aca6828fbf4b51f64828ccd007b56fa64d2788269c074e4a",
    "glyph_0F": "cb049405a8e97c115689dc1ad2be61af6e182158bb6c537fe55e0cb0bc167d84",
    "glyph_10": "930081206b287255b0655b273163c6c15dcb30895067ca4a8ad99eacec75dc1b",
    "glyph_11": "612418a164a39d58f3843f887aae83e62dc3eff62998decfd4c2e98b62470fff",
    "glyph_12": "83226842e2623c3ca46cd4336a7d08256bfb9b08cf0d62feba3b449b3886ed4f",
    "glyph_13": "041179adb7c135d3eecaa89b6073e3bb562e45fc34510187dc6ce5f4012cb9f9",
    "glyph_14": "0c4f3d32f98ea1240da948c9f560103dcc5aa47389354dda4e97ed779ed7d7c8",
    "glyph_15": "c23af644a5c0cdb945b383d926ec3a27765d402201a876172bea514cc4cb5486",
    "glyph_16": "6db925a0b64fc65478081b1905d8f2389829bba2e8b6f093f47c33739e37feae",
    "glyph_17": "ff300659813b80efcf03b44ae921b6df35c7ece78622bf998f1792c911987f60",
    "glyph_18": "5505a059dddc99821e638960f3a59b4ce1c2d8f3d7ae60c30ca665953543018d",
    "glyph_19": "81878da0e6ceb8c56485ba87497c06f71e2ad8add2ad451cc57b9688351b3a16",
    "glyph_1A": "c6685f7ce3520fcbe9afe271bb5e8a781790ed8ca1a063b165bd654a21254269",
    "glyph_1B": "2bc633852055cea290d54e287acc250e4e5b5aa33e5a54779d23ab3438c94d24",
    "glyph_1C": "21e580ea0dc0cc919eabff9a6e1ad5b24e1181705e8df570641ac3a7df99751b",
    "glyph_1D": "1bc41a6e9d026d93a8fba34adafadc62465a5621a80042dfe9637a2299b61bb9",
    "glyph_1E": "a22af12a08df479b8e50591b906407e3c0aa1a201d4df826d2e0f91c108bca57",
    "glyph_1F": "d1e3a254817ed0141065678f8526bf41e05e3740b54cfe6ef4be9ffaa4fddc9e",
    "glyph_20": "f9415605e690ae817751bfa04923fbf93ef6d645aaa5d439949aafe9ca82fade",
    "glyph_21": "5f86060d1ec8454582cfbfcf41a8f65a3b3540ca8b94a03ceb45c27fb917ada6",
    "glyph_22": "f5456ebd73d431aeef849c208057f6605f47181281e531462cde2c80c3e6bfc3",
    "glyph_23": "e12848a2bac2834faa2f45585b3b25fd1b803ad7fea94f125994933dae83a450",
    "glyph_24": "7c720253b993059fb157e3c8f5059202d2030b67b0de334f5cae0eab90171b91",
    "glyph_25": "9e82c0f1fb72a6e2bb3639c9c08daf8115d0009c519fdbab79c69d675086ec14",
    "glyph_26": "af5570f5a1810b7af78caf4bc70a660f0df51e42baf91d4de5b2328de0e83dfc",
    "glyph_27": "b183a91c28955afa713c664a37100415617f0beb383f52668393ebdc1514cce8",
    "glyph_28": "f4ca4d53eb6b3d258f7e38506828b2c3ef7c23bb8931640fe43c0b9e1c138b30",
    "glyph_38": "5ab2adf29dcfed02baea8e44e9df24632e1638344ab346d0c60bf4b0c894b47d",
    "glyph_3F": "7d5d7a2069a0a98729f3193cffebbc5195cadd39fbd9dfb6fa375f0044e45870",
}

# The sprites kept equal to the ROM's (art's module docstring), and why. Every other sprite and glyph must differ.
SOLID = "solid between its extents: nothing is left to choose, and a gap would let a shot skip a pixel row"
PLAY = "decides play: shots strike any lit pixel, explosions erase their own (ours changed 1-12 of 12 games)"
DEBRIS = "decides play: shots falling at a death strike the debris (ours changed 7 of 32 high-score games)"
EXEMPT = {
    "player_blowup0": DEBRIS,
    "player_blowup1": DEBRIS,
    "player_shot": SOLID,
    "shot_explosion": PLAY,
    "squiggly0": PLAY,
    "squiggly1": PLAY,
    "squiggly2": PLAY,
    "squiggly3": PLAY,
    "alien_shot_explosion": PLAY,
    "plunger0": SOLID,
    "plunger1": SOLID,
    "plunger2": SOLID,
    "plunger3": SOLID,
    "rolling0": SOLID,
    "rolling1": PLAY,
    "rolling2": SOLID,
    "rolling3": PLAY,
    "shield": SOLID,
}

# Messages the game part prints: (address, characters printed, text).
MESSAGES = [
    (0x1AA6, 20, "GAME OVER  PLAYER< >"),  # MessageGOver
    (0x1ACF, 20, "ONLY 1PLAYER  BUTTON"),  # Message1Only, printed by the one-credit wait screen
    (0x1AE4, 28, " SCORE<1> HI-SCORE SCORE<2> "),  # MessageScore
    (0x1B70, 14, "PLAY PLAYER<1>"),  # MesssageP1, inside the RAM mirror
    (0x1D94, 12, " 50100150300"),  # SaucSoreStr: the saucer scores, 3 characters each
    (0x1FA9, 7, "CREDIT "),  # MessageCredit
    (0x1FF3, 4, "PUSH"),  # MessagePush, printed by the one-credit wait screen
]
SPLASH_ONLY = [(0x1BA0, 16), (0x1BD0, 16), (0x1F48, 8), (0x1F80, 16), (0x1FB0, 16)]

# The saucer's score (SaucSoreStr) is printed where the saucer flew, and a player shot can strike it: the shot climbs
# 4 pixels a step from Yr 28, so it meets each column of the byte-aligned text in two windows, bits 0-3 and then 4-7.
# Per column of each character the score uses, the halves the ROM's glyph lights (1 the low half, 2 the high half),
# measured from the ROM: ours must light the same, or a shot stops at the text in one program and flies through it in
# the other (the art equivalence experiment caught a bolder font doing so; see the comment above art.FONT).
SCORE_HALVES = {
    " ": (0, 0, 0, 0, 0, 0, 0, 0),
    "0": (0, 3, 3, 3, 3, 3, 0, 0),
    "1": (0, 0, 3, 3, 1, 0, 0, 0),
    "3": (0, 3, 3, 3, 3, 3, 0, 0),
    "5": (0, 3, 3, 3, 3, 3, 0, 0),
}


def tables_bytes(address: int, length: int) -> bytes:
    for start, data in TABLES.items():
        if start <= address and address + length <= start + len(data):
            return data[address - start : address - start + length]
    raise AssertionError(f"no TABLES entry holds {address:04X} (+{length})")


def per_column(name: str) -> int:
    return 2 if name == "shield" else 1


def test_every_sprite_has_the_roms_address_length_and_extents():
    assert set(art.SPRITES) == set(art.EXTENTS)
    assert set(ROM_DIGESTS) == set(art.SPRITES) | {f"glyph_{code:02X}" for code in art.CHARSET}
    for name, (address, data) in art.SPRITES.items():
        rom_address, length, extents = art.EXTENTS[name]
        assert (address, len(data)) == (rom_address, length), name
        assert art.extents(data, per_column(name)) == extents, name


def test_no_sprite_or_glyph_is_the_roms_bitmap_except_the_exempt_ones():
    for name, (_, data) in art.SPRITES.items():
        same = hashlib.sha256(data).hexdigest() == ROM_DIGESTS[name]
        assert same == (name in EXEMPT), name
    for code, data in art.GLYPHS.items():
        same = hashlib.sha256(data).hexdigest() == ROM_DIGESTS[f"glyph_{code:02X}"]
        assert same == (art.CHARSET[code] == " "), art.CHARSET[code]  # the space is blank in any font


@pytest.mark.parametrize("name", sorted(name for name, reason in EXEMPT.items() if reason == SOLID))
def test_the_sprites_exempt_as_solid_are_solid_between_their_extents(name):
    _, data = art.SPRITES[name]
    columns = [int.from_bytes(data[i : i + per_column(name)], "little") for i in range(0, len(data), per_column(name))]
    for value, extent in zip(columns, art.EXTENTS[name][2]):
        assert value == (0 if extent is None else (2 << extent[1]) - (1 << extent[0])), name  # low to high all lit


def test_alien_shots_strike_our_cannon_wherever_they_strike_its_outline():
    """The cannon is the ROM's outline, solid between its extents, with a hatch of ours (art's docstring): no alien
    shot picture or alien-shot explosion, at any offset, overlaps only the hatch, so a shot strikes ours exactly where
    it strikes the solid cannon."""

    def lit(data: bytes) -> set[tuple[int, int]]:
        return {(x, y) for x, byte in enumerate(data) for y in range(8) if byte >> y & 1}

    ours = lit(art.SPRITES["player"][1])
    extents = art.EXTENTS["player"][2]
    solid = {(x, y) for x, extent in enumerate(extents) if extent for y in range(extent[0], extent[1] + 1)}
    assert ours < solid
    shots = [name for name in art.SPRITES if name.startswith(("squiggly", "plunger", "rolling"))]
    for name in [*shots, "alien_shot_explosion"]:
        shot = lit(art.SPRITES[name][1])
        for dx in range(-8, 17):
            for dy in range(-8, 9):
                moved = {(x + dx, y + dy) for x, y in shot}
                assert bool(moved & ours) == bool(moved & solid), (name, dx, dy)


def test_regions_are_the_sprites_and_the_glyph_slots_and_never_overlap():
    glyph_slots = {0x1E00 + code * 8 for code in art.CHARSET}
    assert set(art.SHIP_ART) == {address for address, _ in art.SPRITES.values()} | glyph_slots
    spans = sorted((address, address + len(data)) for address, data in art.SHIP_ART.items())
    assert all(end <= start for (_, end), (start, _) in zip(spans, spans[1:]))
    assert spans[0][0] >= 0x1C00 and spans[-1][1] <= 0x2000
    for start, length in SPLASH_ONLY:
        assert all(end <= start or start + length <= begin for begin, end in spans), hex(start)


def test_the_image_holds_our_art_beside_the_tables():
    image = build_image()  # raises if any region overlaps a TABLES entry
    for address, data in art.SHIP_ART.items():
        assert image[address : address + len(data)] == data


def test_charset_is_the_font_order_the_game_part_uses():
    letters = {code: chr(ord("A") + code) for code in range(26)}
    digits = {0x1A + n: str(n) for n in range(10)}
    signs = {0x24: "<", 0x25: ">", 0x26: " ", 0x27: "=", 0x28: "*", 0x38: "?", 0x3F: "-"}
    assert art.CHARSET == letters | digits | signs
    assert set(art.GLYPHS) == set(art.CHARSET)


@pytest.mark.parametrize(("address", "length", "text"), MESSAGES)
def test_the_font_covers_and_spells_every_message(address, length, text):
    codes = tables_bytes(address, length)
    assert set(codes) <= set(art.GLYPHS)
    assert "".join(art.CHARSET[code] for code in codes) == text
    encode = {char: code for code, char in art.CHARSET.items()}
    assert bytes(encode[char] for char in text) == codes


def test_hi_score_is_spelled_with_the_dash_glyph():
    assert bytes([0x07, 0x08, 0x3F, 0x12, 0x02, 0x0E, 0x11, 0x04]) == tables_bytes(0x1AEE, 8)
    assert "".join(art.CHARSET[code] for code in tables_bytes(0x1AEE, 8)) == "HI-SCORE"


def test_the_saucer_score_meets_a_shot_where_the_roms_glyphs_do():
    assert set(MESSAGES[4][2]) == set(SCORE_HALVES)
    code_of = {char: code for code, char in art.CHARSET.items()}
    for char, halves in SCORE_HALVES.items():
        data = art.GLYPHS[code_of[char]]
        assert tuple(bool(byte & 0x0F) | bool(byte & 0xF0) << 1 for byte in data) == halves, char


def test_glyphs_are_distinct_and_keep_their_cell_edges_clear():
    assert len(set(art.GLYPHS.values())) == len(art.GLYPHS)  # no two characters look alike on screen
    for code, data in art.GLYPHS.items():
        assert len(data) == 8 and data[0] == data[7] == 0, art.CHARSET[code]  # a column clear on each side
        assert not any(byte & 0x80 for byte in data), art.CHARSET[code]  # and the top row
        assert any(data) == (art.CHARSET[code] != " "), art.CHARSET[code]  # only the space is blank, as the view needs


def test_pictures_are_rectangles_of_whole_bytes():
    for name, (_, picture) in art.PICTURES.items():
        rows = picture.split()
        assert len(rows) == 8 * per_column(name) and set("".join(rows)) <= set("#."), name
        assert len({len(row) for row in rows}) == 1, name
    assert art.columns("#.\n" + ".#\n" * 7) == bytes([0x80, 0x7F])
    assert art.columns("#\n" + ".\n" * 15) == bytes([0x00, 0x80])  # two bytes per column, low byte first
