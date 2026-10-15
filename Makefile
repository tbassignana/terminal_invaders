.PHONY: test lint run install build clean balance balance-full

test:
	python3 -m pytest

lint:
	python3 -m ruff check terminal_invaders tests invaders.py tools
	python3 -m ruff format --check terminal_invaders tests invaders.py tools

run:
	python3 -m terminal_invaders

install:
	python3 -m pip install -e '.[dev]'

build:
	python3 -m build

balance:
	python3 tools/balance.py --bots skilled sk_d18_p85 naive novice --difficulty normal --seeds 12

balance-full:
	python3 tools/balance.py --bots all --difficulty all --seeds 40
	python3 tools/balance.py --bots skilled --difficulty normal hard --mode endless --seeds 40 --max-minutes 60

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache .hypothesis .coverage* htmlcov build dist *.egg-info
	find terminal_invaders tests tools -type d -name __pycache__ -exec rm -rf {} +
