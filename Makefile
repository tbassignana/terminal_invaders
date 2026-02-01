.PHONY: test test-coverage lint run install clean

test:
	python3 -m pytest test_invaders.py -v

test-coverage:
	python3 -m pytest test_invaders.py --cov=invaders --cov-report=html --cov-report=term-missing
	@echo "HTML coverage report generated in htmlcov/"

lint:
	python3 -m ruff check invaders.py test_invaders.py

run:
	python3 invaders.py

install:
	pip install -e ".[test]"

clean:
	rm -rf __pycache__ .pytest_cache .coverage htmlcov *.egg-info .eggs build dist
	find . -name '*.pyc' -delete
