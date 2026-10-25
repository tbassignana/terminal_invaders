.PHONY: test lint run install build clean balance balance-full balance-act balance-tune balance-gate balance-input \
	balance-sweep frames frames-check palette-check firstrun firstrun-wheel playtest playtest-grade

# `make balance PYTHON=.venv/bin/python JOBS=1` runs the smoke with another interpreter on one process.
PYTHON ?= python3
JOBS ?= 2
# `make balance-sweep` plays the feasibility sweep (spec 22.3) in SHARDS chunks into SWEEP_OUT, then reports it. A
# stopped chunk resumes where it was; `make balance-sweep SHARDS=6` makes the chunks shorter.
SHARDS ?= 3
SWEEP_OUT ?= build/balance-sweep
# The tiers of spec 19.2, each played in resumable chunks (`--shard K/N --out DIR`) and then merged. TUNE_SHARDS,
# GATE_SHARDS and AUDIT_SHARDS (the audit of the gate's taped games, row Q10), and INPUT_SHARDS come from
# `python3 tools/balance.py --tune --plan`, `--gate --plan` and `--gate-input --plan` on the integrated tree
# (2 processes, at most 17 minutes a chunk). TUNE_SHARDS, GATE_SHARDS and AUDIT_SHARDS were last sized after tuning
# round 5, with spec 27.2's Q1 and Q7 (Endless) on Hard and `--lengths` from round 5's run and its 16 Endless games
# on Hard: 3.6 and 8.9 CPU-hours, the audit (with its two plans in a row) 0.3. INPUT_SHARDS is from code change 4's
# plan (5.1 CPU-hours, with round 3's lengths): its rows did not change.
BALANCE_OUT ?= build/balance
ACT ?= 1
ACT_OUT ?= build/balance-act$(ACT)
LOADOUT ?=
TUNE_SHARDS ?= 7
TUNE_OUT ?= build/balance-tune
GATE_SHARDS ?= 16
GATE_OUT ?= build/balance-gate
AUDIT_SHARDS ?= 1
INPUT_SHARDS ?= 9
INPUT_OUT ?= build/balance-input
# ruff's cache, outside the tree (Phase 5 G5.2)
RUFF_CACHE ?= $(or $(TMPDIR),/tmp)/terminal-invaders-ruff
# `make playtest FIXTURE=kitty SEED=4 RUN=build/playtest/kitty-4`: one new-player panel game (spec 15.3), served until
# stopped; the player sends `python3 tools/playtest.py send "CMD"` from this folder (or from PLAYER=FOLDER, so two runs
# at a time each have their own), and `make playtest-grade RUN=...` grades the run.
FIXTURE ?= apple-terminal-26
SEED ?= 1
RUN ?= build/playtest/$(FIXTURE)-$(SEED)

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check --cache-dir $(RUFF_CACHE) terminal_invaders tests invaders.py tools
	$(PYTHON) -m ruff format --check --cache-dir $(RUFF_CACHE) terminal_invaders tests invaders.py tools

run:
	$(PYTHON) -m terminal_invaders

install:
	$(PYTHON) -m pip install -e '.[dev]'

build:
	$(PYTHON) -m build

# The smoke (12 seeds, never judged): every game that loses a life keeps its tape, the fairness audit replays them,
# and the merge exits 1 only on an unavoidable hit or a shot outside the rules (the rates need the gate's games).
# strong is the bot the quality bar's strong rows judge; perfect is skilled, which must win almost every game.
balance:
	rm -rf $(BALANCE_OUT)
	$(PYTHON) tools/balance.py --bots strong perfect sk_d18_p85 naive novice --difficulty normal --seeds 12 --tapes \
		--jobs $(JOBS) --out $(BALANCE_OUT) > /dev/null
	$(PYTHON) tools/balance.py --audit $(BALANCE_OUT) --jobs $(JOBS)
	$(PYTHON) tools/balance.py --merge $(BALANCE_OUT) --check

frames:
	$(PYTHON) tools/make_frames.py

# The goldens are what tests/support/frames.py draws: lists what `make frames` would write, and fails if anything.
frames-check:
	$(PYTHON) tools/make_frames.py --check

# Spec 3.5-3.8 (G5.6, G5.7): the palettes' contrast tables, the shapes at k = 1-6 and the flash check.
palette-check:
	$(PYTHON) tools/palette_check.py

# The scripted new-player checks of spec 15.2 (G5.10): R1-R7 on apple-terminal-26 at 80 x 24 and kitty at 120 x 44,
# through the real loop on the virtual clock (about 3-4 CPU-minutes). The S1-S4 tests carry "wheel" in their names.
firstrun:
	$(PYTHON) -m pytest -p no:cacheprovider -o addopts="" -m gate -k "not wheel" -rA tests/test_firstrun.py

# S1-S4 (G5.11): the wheel and the sdist built from a copy, installed into fresh venvs, then launched on a pty with
# an empty HOME (about 3 minutes, one process). The build needs setuptools >= 77: $(PYTHON)'s, or WHEEL_BUILDER's
# (`make firstrun-wheel WHEEL_BUILDER=python3.13`), or a python3 on PATH that has it.
WHEEL_BUILDER ?=
firstrun-wheel:
	INVADERS_WHEEL_BUILDER="$(WHEEL_BUILDER)" $(PYTHON) -m pytest -p no:cacheprovider -o addopts="" -m gate -k wheel -rA \
		tests/test_firstrun.py

playtest:
	$(PYTHON) tools/playtest.py serve --run $(RUN) --fixture $(FIXTURE) --seed $(SEED) $(if $(PLAYER),--player-folder $(PLAYER))

playtest-grade:
	$(PYTHON) tools/playtest_grade.py $(RUN)/log.jsonl $(RUN)/debrief.json

balance-full:
	$(PYTHON) tools/balance.py --bots all --difficulty all --seeds 40
	$(PYTHON) tools/balance.py --bots strong --difficulty normal hard --mode endless --seeds 40 --max-minutes 60

# `make balance-act ACT=2`: 200 games each of strong and sk_d18_p85 from act 2's first wave with its loadout
# (`LOADOUT=DIR`: the median loadout of DIR's whole runs instead of the spec's), a tuning aid (spec 19.3, 27).
balance-act:
	$(PYTHON) tools/balance.py --staged $(ACT) --bots strong sk_d18_p85 --seeds 200 $(if $(LOADOUT),--loadout $(LOADOUT)) \
		--jobs $(JOBS) --out $(ACT_OUT) > /dev/null
	$(PYTHON) tools/balance.py --merge $(ACT_OUT)

balance-tune:
	for k in $$(seq 1 $(TUNE_SHARDS)); do \
		$(PYTHON) tools/balance.py --tune --shard $$k/$(TUNE_SHARDS) --out $(TUNE_OUT) --jobs $(JOBS) > /dev/null || exit 1; \
	done
	$(PYTHON) tools/balance.py --merge $(TUNE_OUT)

# The gate (spec 23.3, 27, 27.2): the held-out seeds, Q1's (the strong bot on Hard) and P's tapes audited in
# AUDIT_SHARDS chunks, then every row judged (the strong bot's Normal wins and the good bot's Hard ones are reported).
balance-gate:
	for k in $$(seq 1 $(GATE_SHARDS)); do \
		$(PYTHON) tools/balance.py --gate --shard $$k/$(GATE_SHARDS) --out $(GATE_OUT) --jobs $(JOBS) > /dev/null || exit 1; \
	done
	for k in $$(seq 1 $(AUDIT_SHARDS)); do \
		$(PYTHON) tools/balance.py --audit $(GATE_OUT) --shard $$k/$(AUDIT_SHARDS) --jobs $(JOBS) || exit 1; \
	done
	$(PYTHON) tools/balance.py --merge $(GATE_OUT) --check

# Gate row I1 (spec 23.4, 27): strong through each keyed input on seeds 5000-5199 on Normal, judged against its direct
# Normal games on the same seeds (the gate's row Q1 Normal), so it merges with the gate's directory (run
# `make balance-gate` first).
balance-input:
	for k in $$(seq 1 $(INPUT_SHARDS)); do \
		$(PYTHON) tools/balance.py --gate-input --shard $$k/$(INPUT_SHARDS) --out $(INPUT_OUT) --jobs $(JOBS) \
			> /dev/null || exit 1; \
	done
	$(PYTHON) tools/balance.py --merge $(GATE_OUT) $(INPUT_OUT) --check

balance-sweep:
	for k in $$(seq 1 $(SHARDS)); do \
		$(PYTHON) tools/balance.py --sweep --shard $$k/$(SHARDS) --out $(SWEEP_OUT) --jobs $(JOBS) > /dev/null || exit 1; \
	done
	$(PYTHON) tools/balance.py --merge $(SWEEP_OUT)

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache .hypothesis .coverage* coverage.json htmlcov build dist *.egg-info
	rm -rf $(RUFF_CACHE)
	find terminal_invaders tests tools -type d -name __pycache__ -exec rm -rf {} +
