.PHONY: install format lint test demo tf-check

install:
	uv sync

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

test:
	uv run pytest $(TEST)

demo:
	uv run migrate run --source aws --mock

tf-check:
	uv run python -m migrate.tools.render_fixture_terraform build/terraform
	terraform -chdir=build/terraform fmt -check -recursive
	terraform -chdir=build/terraform init -backend=false -input=false
	terraform -chdir=build/terraform validate
