.PHONY: test lint run install clean

test:
	python3 -m pytest

lint:
	python3 -m ruff check terminal_invaders tests invaders.py
	python3 -m ruff format --check terminal_invaders tests invaders.py

run:
	python3 -m terminal_invaders

install:
	python3 -m pip install -e '.[test]'

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov build dist *.egg-info
	find terminal_invaders tests -type d -name __pycache__ -exec rm -rf {} +
