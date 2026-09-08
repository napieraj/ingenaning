.PHONY: sync check test lint type fmt run relay
sync:      ; uv sync --python 3.12 --extra dev
fmt:       ; uv run ruff format .
lint:      ; uv run ruff check .
type:      ; uv run mypy ingenaning
test:      ; uv run pytest -q
check: fmt lint type test
run:       ; uv run aningd --config ./dev/policy.yaml --db ./dev/aning.db
relay:     ; python3 ingenaning/telemetry/relay.py --hot ./dev/hot --cold ./dev/cold --socket ./dev/access.sock
