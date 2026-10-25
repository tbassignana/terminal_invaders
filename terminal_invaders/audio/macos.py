"""macOS sound for the helper: an AudioQueue refilled one 10 ms block per returned buffer, and afplay as the fallback
that plays each cue itself instead of mixing.

The structs and the callback type are plain ctypes definitions, so this module imports on Linux, where the tests drive
it with a fake library. load() opens AudioToolbox by full path, and only when a sink needs it. Every function gets
explicit argtypes and restype; OSStatus is a signed 32-bit int, and every opaque ref is a c_void_p.
"""

import os
import shutil
import subprocess
import tempfile
import time
import wave
from ctypes import (
    CDLL,
    CFUNCTYPE,
    POINTER,
    Structure,
    byref,
    c_char,
    c_double,
    c_int32,
    c_ubyte,
    c_uint32,
    c_void_p,
    memmove,
)

from .protocol import BLOCK_BYTES, BLOCK_FRAMES, CHANNELS, CUES, RATE
from .sink import Sink, SinkError

AUDIO_TOOLBOX = "/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox"
BUFFERS = 5  # AudioQueue buffers of one block each: 50 ms queued after a refill, 40 ms just before it (A2)
BLOCK_MS = BLOCK_FRAMES * 1000 / RATE
STALL = 2.0  # s with buffers in flight and no callback before the queue counts as dead (A18)
FRAME_BYTES = CHANNELS * 2
EFFECT_PLAYERS = 3  # afplay cues at once
# Cues the game re-sends while their sound lasts (Arcade's saucer every 8 frames, its hit every 10, each clip as long):
# afplay runs one of each at a time, since a player outlives its clip and two would take two of the EFFECT_PLAYERS
# from the shots and the march.
RETRIGGERED = frozenset({"saucer", "saucer_hit"})
ZEROS = bytes(BLOCK_BYTES)
_KEEP = []  # callbacks of queues whose Dispose failed: the queue may still call them, so they must never be freed
_LIBS = None


def fourcc(code: str) -> int:
    """A four-character code as Apple's headers spell it: 'lpcm' is 0x6C70636D."""
    return int.from_bytes(code.encode("ascii"), "big")


class ASBD(Structure):
    """AudioStreamBasicDescription (CoreAudioBaseTypes.h): a Float64 and eight UInt32, 40 bytes."""

    _fields_ = [("mSampleRate", c_double), ("mFormatID", c_uint32), ("mFormatFlags", c_uint32)]
    _fields_ += [(name, c_uint32) for name in ("mBytesPerPacket", "mFramesPerPacket", "mBytesPerFrame")]
    _fields_ += [(name, c_uint32) for name in ("mChannelsPerFrame", "mBitsPerChannel", "mReserved")]


class AQBuffer(Structure):
    """AudioQueueBuffer (AudioQueue.h): 56 bytes on 64-bit, each UInt32 before a pointer padded to 8."""

    _fields_ = [
        ("mAudioDataBytesCapacity", c_uint32),
        ("mAudioData", c_void_p),
        ("mAudioDataByteSize", c_uint32),
        ("mUserData", c_void_p),
        ("mPacketDescriptionCapacity", c_uint32),
        ("mPacketDescriptions", c_void_p),
        ("mPacketDescriptionCount", c_uint32),
    ]


# 48 kHz interleaved stereo s16: kAudioFormatLinearPCM with kAudioFormatFlagIsSignedInteger (4) | IsPacked (8) and no
# IsBigEndian (2), so little endian, which is native on both Apple Silicon and Intel. One frame is one packet.
FORMAT = ASBD(float(RATE), fourcc("lpcm"), 0x4 | 0x8, FRAME_BYTES, 1, FRAME_BYTES, CHANNELS, 16, 0)
CALLBACK = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p)  # AudioQueueOutputCallback(user data, queue, buffer)

AT_FUNCTIONS = {  # AudioQueue.h: name -> (restype, argtypes)
    "AudioQueueNewOutput": (c_int32, [POINTER(ASBD), CALLBACK, c_void_p, c_void_p, c_void_p, c_uint32,
                                      POINTER(c_void_p)]),
    "AudioQueueAllocateBuffer": (c_int32, [c_void_p, c_uint32, POINTER(c_void_p)]),
    "AudioQueueEnqueueBuffer": (c_int32, [c_void_p, c_void_p, c_uint32, c_void_p]),
    "AudioQueueStart": (c_int32, [c_void_p, c_void_p]),
    "AudioQueueStop": (c_int32, [c_void_p, c_ubyte]),  # Boolean inImmediate
    "AudioQueueDispose": (c_int32, [c_void_p, c_ubyte]),
}  # fmt: skip


def declare(at):
    """Give every function this module calls its restype and argtypes; returns at."""
    for name, (restype, argtypes) in AT_FUNCTIONS.items():
        function = getattr(at, name)
        function.restype, function.argtypes = restype, argtypes
    return at


def load():
    """AudioToolbox, declared; raises OSError where it does not exist (any system but macOS)."""
    global _LIBS
    if _LIBS is None:
        _LIBS = declare(CDLL(AUDIO_TOOLBOX))
    return _LIBS


class AudioQueueSink(Sink):
    """Plays the mix through an AudioQueue. Its callback runs on the queue's own thread (no run loop is given) and only
    posts ("aq", buffer) to the helper's queue; the helper's loop then calls on_event and feed, which refills."""

    name = "coreaudio"
    low_water = True  # feed() refills every buffer, so the lead that matters is what was left just before it

    def __init__(self, *, post, at=None, clock=time.monotonic) -> None:
        self.post, self.at, self.clock = post, at, clock
        self.queue: int | None = None  # the AudioQueueRef
        self.cb = None  # the CALLBACK instance, kept alive until the queue is disposed
        self.buffers: dict[int, AQBuffer] = {}  # AudioQueueBufferRef -> a view of it
        self.free: list[int] = []  # buffers back from the queue and not yet refilled, in the order they came back
        self.running = False  # started and not stopped
        self.feeding = False  # what the last feed() was told
        self.last = 0.0  # when a buffer last came back, or the queue started

    def open(self) -> None:
        """Make the queue and its buffers, then start it on silence, so a device that cannot play fails here."""
        if self.at is None:
            try:
                self.at = load()
            except OSError:
                raise SinkError("no AudioToolbox") from None
        at, queue = self.at, c_void_p()
        self.cb = CALLBACK(self._on_buffer)
        self._check(at.AudioQueueNewOutput(byref(FORMAT), self.cb, None, None, None, 0, byref(queue)), "NewOutput")
        self.queue = queue.value
        try:
            for _ in range(BUFFERS):
                ref = c_void_p()
                self._check(at.AudioQueueAllocateBuffer(self.queue, BLOCK_BYTES, byref(ref)), "AllocateBuffer")
                self.buffers[ref.value] = AQBuffer.from_address(ref.value)
                self.free.append(ref.value)
            self._fill(None)
            self._start()
        except SinkError:
            self.close()
            raise

    def on_event(self, event) -> None:
        """A buffer came back: it waits on the free list for feed(). All five back while feeding is an underrun."""
        if event[0] != "aq" or event[1] not in self.buffers or event[1] in self.free:
            return  # a buffer of a closed queue, or a repeat
        self.callbacks += 1
        self.last = self.clock()
        self.free.append(event[1])
        if self.feeding and len(self.free) == BUFFERS:
            self.underruns += 1  # A17: the device had nothing queued after this one

    def feed(self, mixer, feeding: bool) -> None:
        """Feeding: refill every free buffer from the mixer (and restart a stopped queue). Not feeding: let the buffers
        come back, and stop the queue once all are back. Either way, buffers in flight with no callback for STALL is
        fatal. Stop, not Pause: a stopped queue starts again like a new one, one buffer per device cycle, while one
        resumed from a pause takes 3 to 5 buffers at once (measured on an M4 Pro) and can leave none queued."""
        if self.queue is None:
            raise SinkError("closed")
        self.feeding = feeding
        if feeding:
            self._fill(mixer)
            if not self.running:
                self._start()
        elif self.running and len(self.free) == BUFFERS:
            self._check(self.at.AudioQueueStop(self.queue, 1), "Stop")
            self.running = False
        if self.running and len(self.free) < BUFFERS and self.clock() - self.last > STALL:
            raise SinkError("stalled")

    def lead_ms(self) -> float | None:
        """Buffers in flight times 10 ms: 50 after a refill, 40 when one has just come back."""
        return None if self.queue is None else (BUFFERS - len(self.free)) * BLOCK_MS

    def close(self) -> None:
        """Stop at once, then dispose (both synchronous, so no callback runs after), and only then drop the callback."""
        queue, self.queue = self.queue, None
        self.buffers.clear()  # Dispose frees them
        self.free.clear()
        self.running = False
        if queue is None:
            return
        try:
            stopped = self.at.AudioQueueStop(queue, 1)
            disposed = self.at.AudioQueueDispose(queue, 1)
        except Exception:
            stopped = disposed = -1
        if stopped or disposed:
            _KEEP.append(self.cb)
        self.cb = None

    def _fill(self, mixer) -> None:
        """Copy a block (silence without a mixer) into each free buffer and enqueue it."""
        while self.free:
            ref = self.free[0]
            buffer = self.buffers[ref]
            memmove(buffer.mAudioData, ZEROS if mixer is None else (c_char * BLOCK_BYTES).from_buffer(mixer.block()),
                    BLOCK_BYTES)  # fmt: skip
            buffer.mAudioDataByteSize = BLOCK_BYTES
            self._check(self.at.AudioQueueEnqueueBuffer(self.queue, ref, 0, None), "EnqueueBuffer")
            self.free.pop(0)

    def _start(self) -> None:
        self._check(self.at.AudioQueueStart(self.queue, None), "Start")
        self.running = True
        self.last = self.clock()

    def _on_buffer(self, user, queue, buffer) -> None:
        """The queue's callback, on its thread: hand the buffer to the helper's loop. It must never raise."""
        try:
            self.post(("aq", buffer))
        except BaseException:
            pass

    @staticmethod
    def _check(status: int, what: str) -> None:
        if status:
            raise SinkError(f"AudioQueue{what} {status}")


class AfplaySink(Sink):
    """The fallback, which mixes nothing: afplay plays each cue's WAV, at most EFFECT_PLAYERS at once by rank."""

    name = "afplay"
    mixes = False

    def __init__(self, *, cues, player="/usr/bin/afplay", popen=subprocess.Popen, clock=time.monotonic) -> None:
        self.cues, self.player, self.popen, self.clock = cues, player, popen, clock
        self.dir: str | None = None
        self.wavs: list[str] = []  # by rank
        self.effects: list[tuple[int, subprocess.Popen]] = []  # (rank, player), oldest first

    def open(self) -> None:
        """Write each cue as a 48 kHz mono 16-bit WAV into a private temp dir."""
        if not os.access(self.player, os.X_OK):
            raise SinkError("no afplay")
        self.dir = tempfile.mkdtemp(prefix="invaders-audio-")
        try:
            for (name, *_), samples in zip(CUES, self.cues, strict=True):
                self.wavs.append(os.path.join(self.dir, name + ".wav"))
                with wave.open(self.wavs[-1], "wb") as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(RATE)
                    out.writeframes(samples[0::2].tobytes())  # the left channel; both are the same
        except (OSError, wave.Error):
            self.close()
            raise SinkError("cannot write the cues") from None

    def cue(self, rank: int) -> None:
        """Start a player for the cue; with all busy, it replaces the lowest-ranked (oldest among equals) if it outranks
        that one, and is dropped otherwise. A RETRIGGERED cue is dropped while its last player still plays."""
        self._reap()
        if CUES[rank][0] in RETRIGGERED and any(busy == rank for busy, _ in self.effects):
            return
        if len(self.effects) >= EFFECT_PLAYERS:
            lowest = max(self.effects, key=lambda effect: effect[0])  # max keeps the first, so the oldest
            if rank >= lowest[0]:
                return
            self.effects.remove(lowest)
            self._kill(lowest[1])
        self.effects.append((rank, self._spawn([self.player, self.wavs[rank]])))

    def feed(self, mixer, feeding: bool) -> None:
        """Reap finished players."""
        self._reap()

    def cut(self) -> None:
        """Silence every cue now playing (M off)."""
        effects, self.effects = self.effects, []
        for _, player in effects:
            self._kill(player)

    def wait(self, feeding: bool) -> float:
        return 0.1

    def close(self) -> None:
        """SIGKILL every player, reap each, and remove the WAVs."""
        effects, self.effects = self.effects, []
        for _, player in effects:
            self._kill(player)
        if self.dir is not None:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None

    def _reap(self) -> None:
        self.effects = [effect for effect in self.effects if effect[1].poll() is None]

    def _spawn(self, argv: list[str]) -> subprocess.Popen:
        try:
            return self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              close_fds=True)  # fmt: skip
        except (OSError, ValueError, subprocess.SubprocessError):
            raise SinkError("cannot run afplay") from None

    @staticmethod
    def _kill(player: subprocess.Popen) -> None:
        try:
            player.kill()
            player.wait(1.0)
        except Exception:
            pass
