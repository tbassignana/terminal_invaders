"""Terminal capabilities: environment hints and probe replies resolved into one pure, total decision."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .keys import Key

TIERS = ("truecolor", "256", "16", "mono")  # Capabilities.color, best first
REMOTE_RTT = 0.030  # a probe round trip slower than this is a remote terminal even without SSH_* hints
TITLE_STACK_FAMILIES = frozenset("kitty ghostty iterm2 wezterm foot xterm vte konsole alacritty".split())
XTVERSION_FAMILIES = frozenset("kitty ghostty iterm2 wezterm foot xterm tmux konsole alacritty contour vte".split())
TRUECOLOR_XTVERSION = XTVERSION_FAMILIES - {"xterm"}  # 5.6 rule 4
TERM_PROGRAMS = {"Apple_Terminal": "apple_terminal", "iTerm.app": "iterm2", "WezTerm": "wezterm"}
TERM_PROGRAMS |= {"ghostty": "ghostty", "vscode": "vscode", "tmux": "tmux"}
TERM_FAMILIES = {"xterm-kitty": "kitty", "xterm-ghostty": "ghostty", "alacritty": "alacritty", "wezterm": "wezterm"}
TERM_FAMILIES |= {"linux": "linux"}
TERM_PREFIXES = ("foot", "tmux", "screen")  # the family is the prefix
TRUECOLOR_PROGRAMS = ("iTerm.app", "WezTerm", "ghostty")
TRUECOLOR_TERMS = ("xterm-kitty", "xterm-ghostty", "alacritty", "foot", "wezterm")
SIXTEEN_PREFIXES = ("screen", "rxvt", "xterm", "vt220", "ansi")
APPLE_TRUECOLOR = 470  # TERM_PROGRAM_VERSION of Terminal.app on macOS 26
COERCED = ("C.UTF-8", "C.utf8", "UTF-8")  # what CPython sets LC_CTYPE to in a C locale (PEP 538)
# ProbeReplies fields left None when unanswered (dsr is a flag); their names make up Capabilities.answered
REPLY_FIELDS = ("kitty_flags", "decrpm_2026", "xtversion", "osc11", "cpr_echo", "cpr_width", "da1")
XTVERSION = re.compile(r"\s*([A-Za-z][A-Za-z0-9]*)[\s(]*([^\s)]*)")


@dataclass(frozen=True, slots=True)
class EnvHints:
    term: str = ""
    colorterm: str = ""
    term_program: str = ""
    term_program_version: str = ""
    lc_terminal: str = ""
    vte_version: str = ""
    konsole_version: str = ""
    wt_session: bool = False
    tmux: bool = False
    sty: bool = False
    ssh: bool = False
    no_color: bool = False
    colorfgbg: str = ""
    utf8_locale: bool | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> EnvHints:
        term = env.get("TERM", "")
        return cls(
            term=term,
            colorterm=env.get("COLORTERM", ""),
            term_program=env.get("TERM_PROGRAM", ""),
            term_program_version=env.get("TERM_PROGRAM_VERSION", ""),
            lc_terminal=env.get("LC_TERMINAL", ""),
            vte_version=env.get("VTE_VERSION", ""),
            konsole_version=env.get("KONSOLE_VERSION", ""),
            wt_session="WT_SESSION" in env,
            tmux="TMUX" in env or term.startswith("tmux"),
            sty="STY" in env or term.startswith("screen"),
            ssh=any(name in env for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")),
            no_color=bool(env.get("NO_COLOR")),
            colorfgbg=env.get("COLORFGBG", ""),
            utf8_locale=_utf8_locale(env),
        )


def user_env(env: Mapping[str, str], utf8_mode: bool) -> dict[str, str]:
    """env without the LC_CTYPE that CPython put there: in a C, unset or missing locale it sets LC_CTYPE=C.UTF-8
    (PEP 538) and turns on UTF-8 mode (PEP 540), which the user's own LC_CTYPE=C.UTF-8 does not."""
    if utf8_mode and not env.get("LC_ALL") and env.get("LC_CTYPE") in COERCED:
        return {name: value for name, value in env.items() if name != "LC_CTYPE"}
    return dict(env)


def _utf8_locale(env: Mapping[str, str]) -> bool | None:
    """The first non-empty of LC_ALL, LC_CTYPE, LANG; None when it names no charset (C, POSIX, en_US)."""
    value = next((env[name] for name in ("LC_ALL", "LC_CTYPE", "LANG") if env.get(name)), "")
    if "utf-8" in value.lower() or "utf8" in value.lower():
        return True
    if "." in value.split("@")[0]:  # an explicit charset that is not UTF-8
        return False
    return None


@dataclass(frozen=True, slots=True)
class ProbeReplies:
    kitty_flags: int | None = None
    decrpm_2026: int | None = None
    xtversion: str | None = None
    osc11: tuple[int, int, int] | None = None  # 8 bits per channel
    cpr_echo: tuple[int, int] | None = None  # (row, col)
    cpr_width: tuple[int, int] | None = None
    dsr: bool = False
    da1: tuple[int, ...] | None = None
    rtt: float | None = None  # end of the probe write to the DA1 reply
    early_keys: tuple[Key, ...] = ()
    raw: bytes = b""  # everything read, for --probe --raw


@dataclass(frozen=True, slots=True)
class Overrides:
    color: str = "auto"  # auto | truecolor | 256 | 16 | mono (--mono arrives here as "mono")
    theme: str = "auto"  # auto | dark | light | terminal
    pacing: str = "auto"  # auto | adaptive | fixed | off (off: no ack queries at all, so clock pacing)
    kitty: bool | None = None
    sync: bool | None = None
    palette: str = "default"  # the palette in force (Phase 5 spec 3.2): mono is the mono tier, as --mono is
    palette_chosen: bool = False  # it came from --palette or the player's settings, so it beats NO_COLOR


@dataclass(frozen=True, slots=True)
class Capabilities:
    family: str
    version: str
    remote: bool
    multiplexer: str | None
    color: str  # truecolor | 256 | 16 | mono
    background: str  # dark | light (detected)
    theme: str  # dark | light (palette in use)
    paint_background: bool
    sync: bool
    kitty_flags: int
    release_events: bool
    utf8: bool
    ambiguous_wide: bool | None
    unicode_blocks: bool
    echoes_queries: bool
    ack: str | None  # cpr | dsr | da1 | None
    esc_timeout: float
    string_timeout: float
    probe_rtt: float | None
    answered: frozenset[str]
    natural_color: str | None = None  # the tier this terminal has without NO_COLOR (None: not probed, as color)

    def row(self) -> str:
        """One-line summary for --probe, F1 and the debug log."""
        where = ("remote" if self.remote else "local") + (f" in {self.multiplexer}" if self.multiplexer else "")
        keys = f"keys kitty {self.kitty_flags}" if self.kitty_flags else "keys legacy"
        if self.release_events:
            keys += " release"
        glyphs = "blocks" if self.unicode_blocks else "utf8" if self.utf8 else "ascii"
        if self.ambiguous_wide:
            glyphs += " ambiguous-wide"
        parts = [f"{self.family} {self.version}".strip(), where, f"{self.color} {self.theme}"]
        if not self.paint_background:
            parts[-1] += " on terminal bg"
        parts += ["sync" if self.sync else "no sync", keys, glyphs, f"ack {self.ack or 'none'}"]
        if self.echoes_queries:
            parts.append("echoes queries")
        parts.append("silent" if self.probe_rtt is None else f"rtt {self.probe_rtt * 1000:.1f}ms")
        return "  ".join(parts)


def resolve(env: EnvHints, replies: ProbeReplies, overrides: Overrides, *, kitty_verified: int = 0) -> Capabilities:
    """Combine env hints, probe replies and flags; kitty_verified holds the flags push_kitty confirmed."""
    if replies.da1 is None:  # silent terminal: env-only capabilities, clock pacing, no teardown sentinel
        replies = ProbeReplies()
    xt = xtversion(replies.xtversion)
    xt_family = xt[0] if xt else None
    family, version = xt or _env_family(env)
    remote = env.ssh or (replies.rtt is not None and replies.rtt > REMOTE_RTT)
    natural = _color(env, xt_family, overrides.color, no_color=False)
    color = _tier(env, xt_family, overrides, natural)
    background = _background(env, replies.osc11)
    width = replies.cpr_width[1] if replies.cpr_width else None
    if env.utf8_locale is False:
        utf8 = False
    elif width is not None:  # the probe printed U+2580: 2 columns narrow, 3 ambiguous-wide, anything else not UTF-8
        utf8 = width in (2, 3)
    else:
        utf8 = family != "linux"
    ambiguous = None if width is None else width == 3
    flags = 0 if overrides.kitty is False else kitty_verified
    answered = {name for name in REPLY_FIELDS if getattr(replies, name) is not None}
    if replies.dsr:
        answered.add("dsr")
    # A CPR's position names the frame it answers (encode.ack_tag), so CPR comes first where the probe's echo CPR
    # came back on its own row; then DSR and DA1, matched in order. DA1 stays the teardown sentinel.
    tagged = replies.cpr_echo is not None and replies.cpr_echo[0] == 2
    ack = "cpr" if tagged else "dsr" if replies.dsr else "da1" if replies.da1 is not None else None
    return Capabilities(
        family=family,
        version=version,
        remote=remote,
        multiplexer="tmux" if env.tmux or xt_family == "tmux" else "screen" if env.sty else None,
        color=color,
        background=background,
        theme=overrides.theme if overrides.theme in ("dark", "light") else background,
        paint_background=color in ("truecolor", "256") and overrides.theme != "terminal",
        sync=replies.decrpm_2026 in (1, 2) if overrides.sync is None else overrides.sync,
        kitty_flags=flags,
        release_events=bool(flags & 2 and flags & 8),
        utf8=utf8,
        ambiguous_wide=ambiguous,
        unicode_blocks=utf8 and ambiguous is not True,
        echoes_queries=replies.cpr_echo is not None and replies.cpr_echo[1] > 1,
        ack=None if overrides.pacing == "off" else ack,
        esc_timeout=0.15 if remote else 0.05,
        string_timeout=2.0 if remote else 0.5,
        probe_rtt=replies.rtt,
        answered=frozenset(answered),
        natural_color=natural,
    )


def xtversion(text: str | None) -> tuple[str, str] | None:
    """(family, version) from an XTVERSION reply such as 'kitty(0.43.1)' or 'iTerm2 3.5.6'; None if unknown."""
    match = XTVERSION.match(text or "")
    if not match or match[1].lower() not in XTVERSION_FAMILIES:
        return None
    return match[1].lower(), match[2]


def _env_family(env: EnvHints) -> tuple[str, str]:
    if env.term_program in TERM_PROGRAMS:
        return TERM_PROGRAMS[env.term_program], env.term_program_version
    if env.lc_terminal == "iTerm2":
        return "iterm2", ""
    if env.vte_version:
        return "vte", env.vte_version
    if env.konsole_version:
        return "konsole", env.konsole_version
    if env.wt_session:
        return "windows_terminal", ""
    if env.term in TERM_FAMILIES:
        return TERM_FAMILIES[env.term], ""
    return next((prefix for prefix in TERM_PREFIXES if env.term.startswith(prefix)), "unknown"), ""


def _tier(env: EnvHints, xt_family: str | None, overrides: Overrides, natural: str) -> str:
    """The tier in force, first match wins (Phase 5 spec 3.2): --mono or --color TIER; the palette mono; under
    NO_COLOR, the tier without it when the palette was chosen (no-color.org: the user's own configuration beats
    NO_COLOR); else what the terminal supports, NO_COLOR giving mono."""
    if overrides.color in TIERS:
        return overrides.color
    if overrides.palette == "mono":
        return "mono"
    if env.no_color and overrides.palette_chosen:
        return natural
    return _color(env, xt_family, overrides.color)


def _color(env: EnvHints, xt_family: str | None, override: str, *, no_color: bool = True) -> str:
    """Colour tier, first match wins (spec 5.6); an explicit tier beats NO_COLOR. no_color=False: the tier the
    terminal would have without NO_COLOR."""
    term = env.term
    if override in TIERS:
        return override
    if env.no_color and no_color:
        return "mono"
    if env.colorterm.lower() in ("truecolor", "24bit") or xt_family in TRUECOLOR_XTVERSION:
        return "truecolor"
    if env.lc_terminal == "iTerm2" or env.term_program in TRUECOLOR_PROGRAMS:
        return "truecolor"
    if env.term_program == "Apple_Terminal":
        build = re.match(r"\d+", env.term_program_version)
        return "truecolor" if build and int(build[0]) >= APPLE_TRUECOLOR else "256"
    if term.endswith("-direct") or term in TRUECOLOR_TERMS:
        return "truecolor"
    if "256" in term:
        return "256"
    if term == "linux" or term.startswith(SIXTEEN_PREFIXES):
        return "16"
    if term in ("vt100", "dumb"):
        return "mono"
    return "256"


def _background(env: EnvHints, osc11: tuple[int, int, int] | None) -> str:
    """OSC 11 when answered (Rec. 709 luma of the 8-bit channels above 0.5), else COLORFGBG's last field 7 or 15."""
    if osc11 is not None:
        r, g, b = osc11
        return "light" if (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.5 else "dark"
    return "light" if env.colorfgbg.rsplit(";", 1)[-1].strip() in ("7", "15") else "dark"
