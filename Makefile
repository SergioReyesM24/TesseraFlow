.PHONY: install run test lint format check

install:
	python -m pip install -e '.[dev]'

run:
	uvicorn main:app --app-dir src --reload

test:
	pytest

lint:
	ruff check .
	mypy src

format:
	ruff format .
	ruff check --fix .

check: lint test

