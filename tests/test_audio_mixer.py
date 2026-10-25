"""The mixer: the cue table (order, timings, the Arcade's event names), bit-exact synthesis (one pinned SHA-256 per
cue), voice priority, the overflow guard, the cut, and the test tone under the voices (spec §5, §9)."""

import hashlib
from array import array

import pytest

from terminal_invaders.arcade.machine import _PORT3_EVENTS, _PORT5_EVENTS
from terminal_invaders.audio.mixer import SILENCE, Mixer, synth
from terminal_invaders.audio.protocol import BLOCK_SAMPLES, CUE_BASE, CUES, CUT, FX_PEAK, QUIT, RANK, cue_byte

# Every cue in rank order: (start Hz, end Hz, length ms, repeat gap ms) and the SHA-256 of whatever the peak-5000
# synthesis gives (§14 A22), the same on 3.11, 3.13 and 3.15. Arcade's cues sit right after boss (blast, the march,
# saucer_hit) and right before fire (saucer), so the campaign's ranks 0-4 never move (Phase 3 spec §7). Phase 4's six
# campaign cues (tell, beam, clear, phase, siren, pick) come after ufo and before saucer (Phase 4 spec 15.6).
GOLDEN = {
    "game_over": ((330, 35, 400, 500), "3703e07b5ad604ce9be02720373287cd59b86c517adb59694f127b0c12727029"),
    "damage": ((220, 45, 240, 250), "3ea856fe29a9fb04d9b5d593d8caf4912dc7c9a89afc3b6533ebb75c3027170c"),
    "bomb": ((95, 25, 320, 400), "a8fd98ff064d11f26db123990ae948586fceb54340582852722dd583367ffb76"),
    "victory": ((330, 1320, 400, 500), "b9adb08155efc19ff9497c4c7b9383323640abd795e953e1422ea605f9154caa"),
    "boss": ((90, 330, 300, 400), "998cde3220f4db9b12fad992ab3e4a44a6615df16088f042d9af23714741a9bc"),
    "blast": ((160, 30, 1500, 1500), "6095c1c5e6685e550d8078d8fdea2d2d1c32550dd85f8cbb93488645e5863745"),
    "march1": ((62, 62, 70, 50), "7b4346f1ef71ad40c0b07c138ab223927e143b5566f50260304fa46b77303993"),
    "march2": ((54, 54, 70, 50), "9b7ff6698499f3aae24cbc6e7a4d6f4e33acc0ba6b23dfcd27e78521cbe7f18b"),
    "march3": ((50, 50, 70, 50), "23285e11f0e92ab2a0e67cea0f5a63c22065de99fabe1d016e71e4758b035868"),
    "march4": ((46, 46, 70, 50), "c577b4273723930ae03f24b7efd903c8eb16900f88151090622a96b85fab0f37"),
    "saucer_hit": ((1488, 106, 166, 50), "4362633a69bafe84f70c6e05ae807dd115c0131bf5176333b37723684135bc38"),
    "life": ((440, 1760, 250, 300), "c45868da2147aa145a3826f3b4c266c181e842b64049df378cef42290b39510c"),
    "wave": ((220, 880, 220, 400), "a9ca679db08dd0fa88ef8d5be0c79b88a883790690ec391fbf4744a13bc22d74"),
    "pickup": ((660, 1320, 140, 150), "e4162db72500f9494f08128a4751f223395b08913ed7d8c7ef5dbf514f65c2c3"),
    "shield": ((1320, 660, 100, 120), "8ed975ab447976a9e591ab8515fa5b68ed13dae98ec6b22a74808af75687a9c8"),
    "kill": ((320, 65, 100, 80), "36f16279f5544b69373773cc9576d1b7a03d628b5a32132889437126b438af82"),
    "hit": ((420, 220, 40, 120), "845fa8a68743b99280bdd51fb6870d14898c799ab9990ef98c786b7641a99f93"),
    "ufo": ((180, 480, 200, 400), "84209eee4fedce00d2a48bd0d56674b7e9437a49a1f27ba0e15a977603cd18f3"),
    "tell": ((1200, 1500, 40, 250), "87112e0aff4112c9651438d82cd509559754dd2f75c28d00fc75fb048d66486a"),
    "beam": ((110, 90, 300, 400), "18dda794e8fa7632bf68a487d51f9c46651cbd605177e7c1b880159e147d55d4"),
    "clear": ((523, 1046, 260, 400), "6aa48912165252a8c718891d36ad84201ebaf2768e6bcd2f5a3e379fa8269a6f"),
    "phase": ((160, 640, 350, 400), "923d219ce0d48e382496cd07dcfaf80e96dd768f0141aa9f4f74dff093151b4d"),
    "siren": ((440, 220, 500, 600), "7a0a91dc88012217613eacb8fbe5d9021fcd7140358c3ae4bf5acd59acfe8109"),
    "pick": ((880, 1320, 150, 200), "70a57aed89638829c1fed43ef3f782ced5ff1ee534c160c5b9b0ddf381ad2d6e"),
    "saucer": ((780, 2000, 133, 50), "2e86d17d8e88390fca14693a117bc86fd9b59845ad3459748cbd9c61c28eeb48"),
    "fire": ((1040, 260, 60, 120), "6c4ef4971b5552fe10e12e2049b1e7b5ecddfdc519d875fbc336a116656eef01"),
}


class Counting:
    """A test tone whose n-th block (from 1) holds 100 * n everywhere: a mixed block tells which read it came from."""

    def __init__(self):
        self.reads = 0

    def read(self):
        self.reads += 1
        return array("h", [100 * self.reads]) * BLOCK_SAMPLES


@pytest.fixture(scope="module")
def cues():
    return [synth(f0, f1, ms) for _name, f0, f1, ms, _gap in CUES]


def test_the_cues_keep_their_ranks_and_the_arcade_sends_only_names_they_have():
    assert [name for name, *_ in CUES] == list(GOLDEN)
    assert {name for _bit, name in _PORT3_EVENTS + _PORT5_EVENTS} <= set(RANK)  # Audio drops an unknown name


def test_the_arcade_cues_sit_where_the_spec_puts_them():
    """Ranks 0-4 stay the campaign's, blast, the march and saucer_hit come right after boss, saucer right before fire
    (Phase 3 spec §7), the campaign's cues keep their order, and Phase 4's new cues follow ufo (Phase 4 spec 15.6),
    so the rows Arcade shares (fire, kill, life) keep their timings and only saucer and fire move rank."""
    names = [name for name, *_ in CUES]
    march = ["march1", "march2", "march3", "march4"]
    assert names[:11] == ["game_over", "damage", "bomb", "victory", "boss", "blast", *march, "saucer_hit"]
    assert names[-2:] == ["saucer", "fire"]
    campaign = ["game_over", "damage", "bomb", "victory", "boss", "life", "wave", "pickup", "shield", "kill", "hit"]
    new = ["tell", "beam", "clear", "phase", "siren", "pick"]
    arcade = {"blast", *march, "saucer_hit", "saucer"}
    assert [name for name in names if name not in arcade] == [*campaign, "ufo", *new, "fire"]
    assert names.index("saucer") == names.index("ufo") + len(new) + 1 and len(CUES) == 26
    shared = {name: GOLDEN[name][0] for name in ("fire", "kill", "life")}
    assert shared == {"fire": (1040, 260, 60, 120), "kill": (320, 65, 100, 80), "life": (440, 1760, 250, 300)}


def test_every_cue_fits_the_helpers_byte_range():
    """A cue byte is CUE_BASE + rank, below the quit byte q, so the helper allows at most 49 cues."""
    assert max(cue_byte(name)[0] for name in RANK) < QUIT[0] and CUT[0] < CUE_BASE


@pytest.mark.parametrize("rank", range(len(CUES)))
def test_each_cue_is_pinned(cues, rank):
    name, *timing = CUES[rank]
    ms, sound = timing[2], cues[rank]
    assert (tuple(timing), hashlib.sha256(sound.tobytes()).hexdigest()) == GOLDEN[name]
    assert len(sound) == 2 * 48 * ms and sound[0] == sound[1] == 0
    assert max(sound) <= FX_PEAK and min(sound) >= -FX_PEAK and sound[0::2] == sound[1::2]


def test_damage_and_game_over_steal_from_the_oldest_lowest_voice(cues):
    mixer = Mixer(cues)
    fire, ufo, damage, game_over = RANK["fire"], RANK["ufo"], RANK["damage"], RANK["game_over"]
    assert [mixer.cue(rank) for rank in (fire, ufo, fire, fire)] == ["fire", "ufo", "fire", "fire"]
    assert mixer.cue(fire) is None  # a fifth fire outranks no voice: dropped
    mixer.block()
    assert mixer.cue(damage) == "damage" and mixer.cue(game_over) == "game_over"
    assert [rank for rank, _pos in mixer.voices] == [ufo, fire, damage, game_over]
    assert mixer.cue(RANK["bomb"]) == "bomb" and mixer.cue(RANK["boss"]) == "boss"  # the last fire, then the ufo
    assert mixer.cue(RANK["life"]) is None and [rank for rank, _pos in mixer.voices] == [damage, game_over, 2, 4]


def test_one_voice_passes_through_padded_and_two_are_summed(cues):
    short = synth(440, 440, 15)  # 720 frames: its second block is zero-padded
    mixer = Mixer([short, cues[1]])
    assert mixer.block() is SILENCE and not mixer.audible
    mixer.cue(0)
    first = mixer.block()
    assert mixer.started == [0] and mixer.audible
    second = mixer.block()
    assert mixer.started == [] and not mixer.audible and mixer.voices == []
    assert first + second == short + array("h", bytes(2 * BLOCK_SAMPLES - 2 * len(short) + 2 * BLOCK_SAMPLES))
    mixer.cue(0)
    mixer.cue(1)
    assert list(mixer.block()) == [a + b for a, b in zip(short[:BLOCK_SAMPLES], cues[1][:BLOCK_SAMPLES])]
    assert mixer.started == [0, 1]


def test_the_guard_clamps_an_overflow():
    loud = array("h", [30000, -30000]) * (BLOCK_SAMPLES // 2)
    mixer = Mixer([loud, loud, loud])
    for rank in range(3):
        mixer.cue(rank)
    assert mixer.block() == array("h", [32767, -32768]) * (BLOCK_SAMPLES // 2)


def test_cut_silences_every_voice_at_once(cues):
    mixer = Mixer(cues)
    mixer.cue(RANK["fire"])
    mixer.cue(RANK["damage"])
    mixer.block()
    mixer.cut()  # "!"
    assert mixer.voices == [] and not mixer.audible and mixer.block() is SILENCE


def test_the_test_tone_plays_under_the_voices_and_outlasts_a_cut(cues):
    mixer = Mixer(cues, tone=Counting())
    assert mixer.audible and mixer.block() == array("h", [100]) * BLOCK_SAMPLES  # no voice: the tone alone
    mixer.cue(RANK["hit"])
    hit = cues[RANK["hit"]][:BLOCK_SAMPLES]
    assert list(mixer.block()) == [200 + sample for sample in hit] and mixer.started == [RANK["hit"]]
    mixer.cut()
    assert mixer.audible and mixer.block() == array("h", [300]) * BLOCK_SAMPLES and mixer.tone.reads == 3
