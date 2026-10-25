"""The interface every audio backend implements, and the error that moves the helper on to the next one."""


class SinkError(Exception):
    """A backend cannot play: the message is a short reason for the status line ("no pw-cat", "stalled")."""


class Sink:
    """Defaults for a backend. Subclasses set name and implement open, feed and close."""

    name = "sink"
    # False: the backend plays cues itself, so the engine calls cue() and cut() instead of feeding a mixer.
    mixes = True
    # True: the engine reads lead_ms() just before feed(), the low-water mark of a sink that refills everything it gets
    # back (an AudioQueue); otherwise just after it.
    low_water = False
    underruns: int | None = 0  # None: the backend cannot see the device run dry (a pipe player reads ahead)
    callbacks = 0

    def open(self) -> None:
        """Start the device or player; bounded; raises SinkError."""

    def feed(self, mixer, feeding: bool) -> None:
        """Write what the pacing rule allows, pulling mixer.block() per block; raises SinkError."""

    def on_event(self, event) -> None:
        """An event a backend posted to the helper's queue (an AudioQueue buffer coming back)."""

    def wait(self, feeding: bool) -> float:
        """Seconds the helper may block on its message queue before the next feed()."""
        return 0.5

    def lead_ms(self) -> float | None:
        """Milliseconds queued ahead of the device now, or None when the backend cannot tell."""
        return None

    def close(self) -> None:
        """Stop at once; bounded, never raises, safe to call twice."""

    def cue(self, rank: int) -> None:
        """Non-mixing backends: start the cue with this rank."""

    def cut(self) -> None:
        """Non-mixing backends: silence every cue now playing."""
