"""The helper in-process (spec §9): Engine on a fake clock with a scripted queue and FakeSinks, the chain per platform
and INVADERS_AUDIO, failover, silent, idle, the stale drop, quit, stats; then the file sink and the test tone."""

import os
import queue
import sys
import types
from array import array

import pytest

from terminal_invaders import audio
from terminal_invaders.audio import helper
from terminal_invaders.audio.helper import Engine, FileSink, Tone, chain, run, status_writer
from terminal_invaders.audio.mixer import Mixer, synth
from terminal_invaders.audio.protocol import BLOCK_SAMPLES, CUES, RANK, cue_byte
from terminal_invaders.audio.sink import Sink, SinkError
from tests.test_audio_mixer import Counting

LINUX = ["pulse-simple", "pw-cat", "pacat", "aplay"]


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class Script:
    """The helper's queue on a fake clock: get() jumps to the next item if it is due within the timeout, else lets the
    timeout pass and raises Empty; past the last item it returns EOF (None)."""

    def __init__(self, clock, items):
        self.clock, self.items = clock, list(items)

    def get(self, timeout):
        if not self.items:
            return self.clock.now, None
        if self.items[0][0] <= self.clock.now + timeout:
            self.clock.now = max(self.clock.now, self.items[0][0])
            return self.items.pop(0)
        self.clock.now += max(timeout, 0.001)  # a real wait always takes some time
        raise queue.Empty

    def get_nowait(self):
        if self.items and self.items[0][0] <= self.clock.now:
            return self.items.pop(0)
        raise queue.Empty


class FakeSink(Sink):
    """Pulls one block per feed while feeding, as a 10 ms pipe would; fail_after blocks, then SinkError("stalled")."""

    def __init__(self, name, *, fail_open=None, fail_after=None, mixes=True, clock=None):
        self.name, self.fail_open, self.fail_after, self.mixes, self.clock = name, fail_open, fail_after, mixes, clock
        self.blocks, self.feeds, self.calls, self.closed, self.lead = [], [], [], 0, None

    def open(self):
        if self.fail_open:
            raise SinkError(self.fail_open)

    def feed(self, mixer, feeding):
        self.feeds.append((self.clock() if self.clock else None, feeding))
        if feeding:
            if len(self.blocks) == self.fail_after:
                raise SinkError("stalled")
            self.blocks.append(mixer.block())

    def wait(self, feeding):
        return 0.01 if feeding else 0.5

    def lead_ms(self):
        return self.lead

    def on_event(self, event):
        self.calls.append(event)
        if event == ("aq", 0):
            raise SinkError("bad buffer")

    def close(self):
        self.closed += 1

    def cue(self, rank):
        self.calls.append(("cue", rank))

    def cut(self):
        self.calls.append(("cut",))


@pytest.fixture(scope="module")
def cues():
    return [synth(f0, f1, ms) for _name, f0, f1, ms, _gap in CUES]


def engine_with(*sinks, mixer, clock=None, **kwargs):
    table, lines = {sink.name: sink for sink in sinks}, []
    engine = Engine(
        mixer, list(table), say=lines.append, make=lambda name, _e: table[name], clock=clock or Clock(), **kwargs
    )
    return engine, lines


def fake_modules(monkeypatch, **modules):
    """Stand-ins for audio.linux and audio.macos, found by the helper's lazy `from . import NAME`."""
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, f"terminal_invaders.audio.{name}", module)
        monkeypatch.setattr(audio, name, module, raising=False)


@pytest.mark.parametrize(
    "platform, setting, names",
    [
        ("linux", None, LINUX),
        ("linux", "", LINUX),
        ("darwin", None, ["coreaudio", "afplay"]),
        ("darwin", "none", []),
        ("linux", "pacat, aplay", ["pacat", "aplay"]),
        ("darwin", "afplay", ["afplay"]),
        ("linux", "file:/tmp/a,b.raw", ["file:/tmp/a,b.raw"]),
    ],
)
def test_chain_per_platform_and_setting(platform, setting, names):
    assert chain(platform, setting) == names


def test_backends_are_built_by_name_through_lazy_imports(monkeypatch):
    made = []

    def recorder(kind):
        return lambda *args, **kwargs: made.append((kind, args, kwargs))

    linux = types.SimpleNamespace(PulseSink=recorder("pulse"), PipeSink=recorder("pipe"))
    macos = types.SimpleNamespace(AudioQueueSink=recorder("aq"), AfplaySink=recorder("afplay"))
    fake_modules(monkeypatch, linux=linux, macos=macos)
    clock, post, say, mixer = Clock(), object(), object(), types.SimpleNamespace(cues=["cue"])
    engine = types.SimpleNamespace(clock=clock, post=post, say=say, mixer=mixer)
    for name in [*LINUX, "coreaudio", "afplay"]:
        helper.make(name, engine)
    assert made == [
        ("pulse", (), {"clock": clock}),
        *[("pipe", (name,), {"clock": clock}) for name in LINUX[1:]],
        ("aq", (), {"post": post, "clock": clock}),
        ("afplay", (), {"cues": ["cue"], "clock": clock}),
    ]
    sink = helper.make("file:/x/y.raw", engine)
    assert isinstance(sink, FileSink) and (sink.path, sink.name, sink.clock) == ("/x/y.raw", "file", clock)
    with pytest.raises(SinkError, match="unknown backend"):
        helper.make("jack", engine)


def test_failover_at_open(cues):
    a, b = FakeSink("a", fail_open="no a"), FakeSink("b")
    engine, lines = engine_with(a, b, mixer=Mixer(cues))
    assert lines == ["fallback a: no a", "backend b"] and engine.sink is b and a.closed == 1


def test_failover_mid_run_keeps_the_voices_and_the_tone(cues):
    a, b, clock = FakeSink("a", fail_after=3), FakeSink("b"), Clock()
    mixer = Mixer(cues, tone=Counting())
    engine, lines = engine_with(a, b, mixer=mixer, clock=clock)
    run(engine, Script(clock, [(0.0, cue_byte("bomb")), (0.1, b"q")]))
    assert lines == ["backend a", "fallback a: stalled", "backend b"] and a.closed == 1
    reference = Mixer(cues, tone=Counting())
    reference.cue(RANK["bomb"])
    blocks = a.blocks + b.blocks
    assert len(a.blocks) == 3 and len(blocks) >= 9 and mixer.tone.reads == len(blocks)
    assert blocks == [reference.block() for _ in blocks]  # as if no sink had failed


def test_every_sink_failing_says_silent_and_ends_the_loop(cues):
    a, b, clock = FakeSink("a", fail_open="no a"), FakeSink("b", fail_after=1), Clock()
    engine, lines = engine_with(a, b, mixer=Mixer(cues), clock=clock)
    run(engine, Script(clock, [(0.0, cue_byte("fire")), (5.0, b"q")]))
    assert lines == ["fallback a: no a", "backend b", "fallback b: stalled", "silent: every backend failed"]
    assert engine.quit and engine.sink is None and clock.now < 1.0  # the loop ended long before the "q"
    engine, lines = engine_with(mixer=Mixer(cues))
    assert lines == ["silent: no backend"] and engine.quit and engine.wait() == 0.0
    engine.feed()


def test_idle_two_seconds_after_the_last_audible_block_and_resume_at_once(cues):
    clock = Clock()
    sink = FakeSink("a", clock=clock)
    engine, _lines = engine_with(sink, mixer=Mixer(cues), clock=clock)
    run(engine, Script(clock, [(0.0, cue_byte("fire")), (5.0, cue_byte("hit")), (6.0, b"q")]))
    stop = next(t for t, feeding in sink.feeds if not feeding)
    resume = next(t for t, feeding in sink.feeds if feeding and t > stop)
    assert 2.05 <= stop <= 2.1 and resume == 5.0  # fire lasts 60 ms; feeding stays on 2 s after
    assert len([t for t, feeding in sink.feeds if not feeding and t < resume]) <= 7  # idle turns wait 0.5 s
    assert sink.blocks[:6] == [cues[RANK["fire"]][i : i + BLOCK_SAMPLES] for i in range(0, 6 * BLOCK_SAMPLES, 960)]


def test_stale_cues_are_dropped_and_a_cut_still_applies(cues):
    mixer = Mixer(cues)
    engine, _lines = engine_with(FakeSink("a"), mixer=mixer, clock=Clock(10.0))
    mixer.cue(RANK["damage"])
    engine.handle(9.85, b"!" + cue_byte("fire"))
    assert mixer.voices == []  # the cut applied to the damage voice, and the late fire after it dropped
    engine.handle(9.95, cue_byte("fire") + b"!" + cue_byte("hit") + bytes([0x20, 0x7F]))
    assert [rank for rank, _pos in mixer.voices] == [RANK["hit"]]


def test_each_stale_cue_is_named_on_a_status_line(cues):
    mixer = Mixer(cues)
    engine, lines = engine_with(FakeSink("a"), mixer=mixer, clock=Clock(10.0))
    engine.handle(9.85, cue_byte("fire") + cue_byte("damage"))
    assert lines == ["backend a", "stale fire", "stale damage"]
    assert mixer.voices == []  # named, and not played


@pytest.mark.parametrize("script", [[(0.0, cue_byte("fire") + b"q" + cue_byte("hit"))], []])
def test_quit_on_q_and_on_eof(cues, script):
    clock, sink, mixer = Clock(), FakeSink("a"), Mixer(cues)
    engine, _lines = engine_with(sink, mixer=mixer, clock=clock)
    run(engine, Script(clock, script))
    assert engine.quit and sink.feeds == [] and [rank for rank, _pos in mixer.voices] == [RANK["fire"]] * len(script)
    engine.close()
    engine.close()
    assert sink.closed == 1 and engine.sink is None


def test_stats_every_two_seconds_while_feeding(cues):
    clock, cpu, sink = Clock(), Clock(), FakeSink("a")
    mixer = Mixer(cues, tone=Tone(440))  # audible all along
    engine, lines = engine_with(sink, mixer=mixer, clock=clock, cpu=cpu)
    for i, lead in enumerate([30.0, 50.0, 10.0, 40.0, 20.0, None, None, None, None]):
        clock.now, cpu.now, sink.lead, sink.underruns, sink.callbacks = i * 0.5, i * 0.01, lead, i, 2 * i
        engine.feed()
    assert lines[1:] == [
        "stats cpu=20.0 lead=10.0/10.0/30.0/50.0 underruns=4 callbacks=8",
        "stats cpu=20.0 lead=- underruns=4 callbacks=8",
    ]
    mixer.tone = None  # quiet after the last feed, at 4.0
    clock.now = 7.0
    engine.feed()  # idle: no stats, and the window starts afresh when feeding resumes
    assert len(lines) == 3 and engine.window is None
    engine.close()


def test_a_low_water_sink_reports_what_was_left_before_each_refill(cues):
    class Queue(FakeSink):
        low_water = True

        def feed(self, mixer, feeding):
            super().feed(mixer, feeding)
            self.lead = 50.0  # every buffer refilled

    clock, sink, mixer = Clock(), Queue("q"), Mixer(cues, tone=Tone(440))
    engine, lines = engine_with(sink, mixer=mixer, clock=clock, cpu=Clock())
    for i, left in enumerate([0.0, 40.0, 30.0, 40.0, 40.0]):  # 0: all back after an idle, not sampled
        clock.now, sink.lead = i * 0.5, left
        engine.feed()
    assert lines[1:] == ["stats cpu=0.0 lead=30.0/30.0/40.0/40.0 underruns=0 callbacks=0"]


def test_a_sink_that_cannot_see_underruns_reports_a_dash(cues):
    clock, sink, mixer = Clock(), FakeSink("a"), Mixer(cues, tone=Tone(440))
    sink.underruns = None
    engine, lines = engine_with(sink, mixer=mixer, clock=clock, cpu=Clock())
    for i in range(5):
        clock.now = i * 0.5
        engine.feed()
    assert lines[1:] == ["stats cpu=0.0 lead=- underruns=- callbacks=0"]


class Refusing(FakeSink):
    """A non-mixing sink whose player cannot be started for a cue."""

    def __init__(self, name):
        super().__init__(name, mixes=False)

    def cue(self, rank):
        raise SinkError("cannot run afplay")


def test_a_non_mixing_sink_failing_on_a_cue_fails_over(cues):
    a, b, c = FakeSink("a", fail_after=0), Refusing("b"), FakeSink("c")
    engine, lines = engine_with(a, b, c, mixer=Mixer(cues))
    engine.handle(0.0, cue_byte("fire"))
    engine.feed()  # a stalls; b opens
    engine.handle(0.0, b"!" + cue_byte("hit"))  # M off reaches b's players; then b cannot start a cue
    assert lines == ["backend a", "fallback a: stalled", "backend b", "fallback b: cannot run afplay", "backend c"]
    assert b.calls == [("cut",)] and engine.sink is c


def test_a_non_mixing_sink_gets_the_cues_and_the_cut(cues):
    a, b = FakeSink("a", fail_after=1), FakeSink("b", mixes=False)
    engine, lines = engine_with(a, b, mixer=Mixer(cues))
    engine.handle(0.0, cue_byte("fire"))
    engine.feed()
    engine.feed()
    engine.handle(0.0, cue_byte("hit") + b"!")
    engine.feed()
    assert lines == ["backend a", "fallback a: stalled", "backend b"]
    assert b.calls == [("cue", RANK["hit"]), ("cut",)] and b.feeds == [(None, False)]


def test_sink_events_reach_the_sink_and_a_failing_one_fails_over(cues):
    a, b = FakeSink("a"), FakeSink("b")
    engine, lines = engine_with(a, b, mixer=Mixer(cues))
    engine.handle(0.0, ("aq", 5))
    engine.handle(0.0, ("aq", 0))
    assert a.calls == [("aq", 5), ("aq", 0)] and lines == ["backend a", "fallback a: bad buffer", "backend b"]


def test_status_lines_are_ascii_and_dropped_when_the_pipe_is_full():
    r, w = os.pipe()
    os.set_blocking(w, False)
    os.set_blocking(r, False)
    say = status_writer(w)
    say("backend pulse-simple")
    say("error x\ny é")
    assert os.read(r, 100) == b"backend pulse-simple\nerror x y ?\n"
    for chunk in (b"x" * 4096, b"x"):
        try:
            while True:
                os.write(w, chunk)
        except BlockingIOError:
            pass
    say("dropped")  # EAGAIN: no exception, no partial line
    data = b""
    try:
        while True:
            data += os.read(r, 65536)
    except BlockingIOError:
        pass
    assert data and set(data) == {ord("x")}
    os.close(r)
    say("gone")  # EPIPE: ignored
    os.close(w)


def test_file_sink_paces_in_real_time_and_marks_each_voice(tmp_path, cues):
    path = tmp_path / "out.raw"
    path.write_bytes(bytes(400))  # 100 frames already there: the sink appends
    clock, lines, mixer = Clock(), [], Mixer(cues)
    sink = FileSink(str(path), clock=clock, say=lines.append)
    sink.open()
    mixer.cue(RANK["hit"])  # 40 ms: 4 blocks
    sink.feed(mixer, True)
    assert path.stat().st_size == 400 + 3 * 1920 and lines == ["voice hit 100"]  # 30 ms ahead of the clock
    clock.now = 0.015
    sink.feed(mixer, True)
    assert path.stat().st_size == 400 + 5 * 1920 and sink.wait(True) == pytest.approx(0.005)
    mixer.cue(RANK["fire"])
    clock.now = 0.025
    sink.feed(mixer, True)
    assert lines == ["voice hit 100", "voice fire 2500"]
    data = path.read_bytes()[400:]
    assert data[: 4 * 1920] == cues[RANK["hit"]].tobytes() and data[4 * 1920 : 5 * 1920] == bytes(1920)
    assert data[5 * 1920 :] == cues[RANK["fire"]][:BLOCK_SAMPLES].tobytes() == data[10000 - 400 :]
    clock.now = 5.0
    sink.feed(mixer, False)  # idle: nothing written
    assert path.stat().st_size == 400 + 6 * 1920 and sink.wait(False) == 0.5
    sink.feed(mixer, True)  # resumes from now, with no gap filled
    assert path.stat().st_size == 400 + 9 * 1920 and sink.lead_ms() == 0.0 and sink.underruns == 0
    os.close(sink.fd)
    clock.now = 6.0
    with pytest.raises(SinkError):
        sink.feed(mixer, True)
    sink.close()
    sink.close()
    with pytest.raises(SinkError, match="directory"):
        FileSink(str(tmp_path)).open()


def test_the_test_tone_is_an_exact_looping_sine():
    tone = Tone(440)
    second = array("h")
    for _ in range(100):
        block = tone.read()
        assert len(block) == BLOCK_SAMPLES
        second += block
    left = second[0::2]
    assert left == second[1::2] and 7990 <= max(left) <= 8000 and min(left) >= -8000 and left[0] == 0
    rising = sum(1 for x, y in zip(left, left[1:]) if x < 0 <= y)
    assert abs(rising - 440) <= 1 and tone.read() == second[:BLOCK_SAMPLES]  # one second on, the same samples
    assert len(Tone(7).read()) == BLOCK_SAMPLES
    for hz in (0, 24000):
        with pytest.raises(ValueError):
            Tone(hz)
