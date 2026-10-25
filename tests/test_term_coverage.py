"""tools/term_coverage.py holds every terminal_invaders/term and terminal_invaders/campaign module to 90% (spec 11.6,
Phase 4 §23) from coverage's JSON report."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "term_coverage.py"
BOTH = {"terminal_invaders/term/keys.py": 95.0, "terminal_invaders/campaign/game.py": 95.0}


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("term_coverage_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]


def report(**percents):
    return {"files": {name: {"summary": {"percent_covered": value}} for name, value in percents.items()}}


def write(tmp_path, data):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_only_term_and_campaign_modules_under_the_floor_fail(tool):
    data = report(
        **{
            "terminal_invaders/term/decode.py": 100.0,
            "terminal_invaders/term/tty.py": 89.94,
            "terminal_invaders\\term\\probe.py": 72.0,
            "terminal_invaders/campaign/boss.py": 88.0,
            "terminal_invaders/campaign/game.py": 90.0,
            "terminal_invaders/app.py": 40.0,
            "terminal_invaders/term_extra/x.py": 10.0,
            "terminal_invaders/legacy/campaign1.py": 10.0,
            "terminal_invaders/campaign/inner/x.py": 10.0,
        }
    )
    assert tool.short(data) == [
        "terminal_invaders/campaign/boss.py: 88.0% < 90%",
        "terminal_invaders/term/tty.py: 89.9% < 90%",
        "terminal_invaders\\term\\probe.py: 72.0% < 90%",
    ]
    assert tool.short(report(**BOTH)) == []


def test_a_report_without_term_or_campaign_modules_fails(tool, tmp_path, capsys):
    assert tool.main([write(tmp_path, report(**{"terminal_invaders/app.py": 100.0}))]) == 1
    err = capsys.readouterr().err
    assert "no terminal_invaders/term modules" in err and "no terminal_invaders/campaign modules" in err
    assert tool.short(report(**{"terminal_invaders/term/keys.py": 95.0})) == [
        "no terminal_invaders/campaign modules in the report"
    ]


def test_main_exits_by_the_floor(tool, tmp_path, capsys):
    assert tool.main([write(tmp_path, report(**BOTH))]) == 0
    assert "at least 90%" in capsys.readouterr().out
    assert tool.main([write(tmp_path, report(**BOTH | {"terminal_invaders/term/keys.py": 80.0}))]) == 1
    assert capsys.readouterr().err == "terminal_invaders/term/keys.py: 80.0% < 90%\n"


NEW = {"terminal_invaders/fx.py": 93.0, "terminal_invaders/settings.py": 91.0, "terminal_invaders/term/pixels.py": 97.0}


def test_also_holds_the_named_modules_to_min(tool):
    data = report(**BOTH | NEW)
    assert tool.short(data, also=["fx", "settings", "term.pixels"], also_min=90) == []
    assert tool.short(data, also=["fx", "settings"], also_min=92) == ["terminal_invaders/settings.py: 91.0% < 92%"]
    windows = report(**BOTH | {"C:\\src\\terminal_invaders\\fx.py": 80.0})
    assert tool.short(windows, also=["fx"]) == ["C:\\src\\terminal_invaders\\fx.py: 80.0% < 90%"]


def test_also_fails_on_a_module_the_report_lacks(tool):
    data = report(**BOTH | {"terminal_invaders/fxs.py": 99.0, "terminal_invaders/term/fx.py": 99.0})
    assert tool.short(data, also=["fx", "tutorial"]) == [
        "no terminal_invaders/fx.py in the report",
        "no terminal_invaders/tutorial.py in the report",
    ]


def test_main_takes_also_and_min(tool, tmp_path, capsys):
    path = write(tmp_path, report(**BOTH | NEW))
    assert tool.main([path, "--also", "fx", "settings", "--min", "90"]) == 0
    assert "fx, settings covered at least 90%" in capsys.readouterr().out
    assert tool.main([path, "--also", "fx", "settings", "attract", "--min", "92"]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "no terminal_invaders/attract.py in the report",
        "terminal_invaders/settings.py: 91.0% < 92%",
    ]
    assert tool.main([path]) == 0  # without --also, the fixed floors only
