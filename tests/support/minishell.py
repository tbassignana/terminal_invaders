"""A job-control shell in miniature, for the Ctrl-Z / fg / Ctrl-C / Ctrl-\\ tests.

Started as a session leader with a pty slave as stdin, it takes the slave as its controlling terminal and runs
the game as a foreground job in its own process group, as zsh or bash would (or, with --background, as a job
started with &). It reports JSON lines on the EVENTS fd: the game's pid, each stop (with what a shell finds then:
the termios settings it had, nothing left to read) and the exit. A line on the COMMANDS fd answers a stop: "fg"
brings the game to the foreground and continues it, "bg" continues it in the background, and "kill" does what
bash's kill %1 does to a stopped job: SIGTERM, then SIGCONT, leaving it in the background. After the exit it
stays until the COMMANDS fd ends: on macOS a session leader's exit revokes its controlling terminal, and every fd
open on the slave, the test's own too, could then no longer be read.

    python minishell.py EVENTS COMMANDS [--background] -- python -m terminal_invaders ...
"""

import fcntl
import json
import os
import signal
import struct
import sys
import termios

JOB_SIGNALS = (signal.SIGTTOU, signal.SIGTTIN, signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP)


def state(tty: int, original: list) -> dict:
    """What a shell finds: its termios settings, and the bytes left to read (counted with ICANON off, as canonical
    mode counts only complete lines)."""
    attrs = termios.tcgetattr(tty)
    pendin = getattr(termios, "PENDIN", 0)
    same = [*attrs[:3], attrs[3] & ~pendin, *attrs[4:]] == [*original[:3], original[3] & ~pendin, *original[4:]]
    termios.tcsetattr(tty, termios.TCSANOW, [*attrs[:3], attrs[3] & ~termios.ICANON, *attrs[4:6], list(attrs[6])])
    try:
        waiting = struct.unpack("i", fcntl.ioctl(tty, termios.FIONREAD, b"\0\0\0\0"))[0]
    finally:
        termios.tcsetattr(tty, termios.TCSANOW, attrs)
    return {"termios": same, "pending": waiting}


def main() -> int:
    events, commands, argv = int(sys.argv[1]), int(sys.argv[2]), sys.argv[sys.argv.index("--") + 1 :]
    background = "--background" in sys.argv[3 : sys.argv.index("--")]
    tty = 0
    fcntl.ioctl(tty, termios.TIOCSCTTY, 0)
    for signum in JOB_SIGNALS:
        signal.signal(signum, signal.SIG_IGN)
    original = termios.tcgetattr(tty)
    pid = os.fork()
    if pid == 0:
        os.setpgid(0, 0)
        if not background:
            os.tcsetpgrp(tty, os.getpgrp())  # the job is in the foreground before it runs a line
        for signum in JOB_SIGNALS:
            signal.signal(signum, signal.SIG_DFL)
        os.close(events)
        os.close(commands)
        os.execv(argv[0], argv)
    try:
        os.setpgid(pid, pid)
    except PermissionError:
        pass  # the child already exec'd, after setting its own group
    if not background:
        os.tcsetpgrp(tty, pid)

    def report(**fields) -> None:
        os.write(events, (json.dumps(fields) + "\n").encode())

    report(started=pid)
    with os.fdopen(commands) as orders:
        while True:
            _, status = os.waitpid(pid, os.WUNTRACED)
            if os.WIFSTOPPED(status):
                os.tcsetpgrp(tty, os.getpgrp())
                report(stopped=signal.Signals(os.WSTOPSIG(status)).name, **state(tty, original))
                order = orders.readline().strip()
                if order == "kill":
                    os.killpg(pid, signal.SIGTERM)
                elif order == "fg":
                    os.tcsetpgrp(tty, pid)
                elif order != "bg":
                    os.killpg(pid, signal.SIGKILL)
                os.killpg(pid, signal.SIGCONT)
                continue
            report(exit=os.waitstatus_to_exitcode(status), **state(tty, original))
            orders.read()  # until the test is done with the terminal
            return 0


if __name__ == "__main__":
    sys.exit(main())
