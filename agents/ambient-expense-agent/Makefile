.PHONY: install playground run

install:
	uv sync --all-extras

playground:
	uv run agents-cli playground

run:
	uv run python -m expense_agent.fast_api_app

generate-traces:
	uv run python tests/eval/generate_traces.py

grade:
	uv run agents-cli eval grade --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml

