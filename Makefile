HOST ?= 127.0.0.1
PORT ?= 8000

.DEFAULT_GOAL := help

.PHONY: help api api-lan worker schema openapi gen-client web web-stack migrate test test-unit test-e2e \
        sidecars stack-up stack-down stack-build stack-ps stack-logs stack-migrate stack-cli nginx

help:
	@echo "make api        serve the API on $(HOST):$(PORT) (override HOST=/PORT=)"
	@echo "make api-lan    serve on 0.0.0.0 for devices on the LAN (glasses/miniapp dev)"
	@echo "make worker     run the processing pipelines (docs/processing-pipelines.md)"
	@echo "make web        run the web frontend dev server against 'make api' (same HOST=/PORT=)"
	@echo "make web-stack  run the dev server against the deployed stack instead of 'make api'"
	@echo "make sidecars   start just the model containers, for the flow above"
	@echo "make schema     print the streaming frame schema (served at /stream-schema.json)"
	@echo "make openapi    write the OpenAPI spec to frontend/openapi.json"
	@echo "make gen-client regenerate the frontend's typed API client from the spec"
	@echo "make migrate    apply alembic migrations"
	@echo "make test       run the whole test suite (e2e needs Docker)"
	@echo "make test-unit  fast tests only, no Docker"
	@echo "make test-e2e   containerized end-to-end suite"
	@echo ""
	@echo "deployment (deploy/docker-compose.yml, needs deploy/.env):"
	@echo "make stack-up      build and start the whole stack"
	@echo "make stack-down    stop it (weights survive: the caches are external)"
	@echo "make stack-build   build the images without starting anything"
	@echo "make stack-ps      service status"
	@echo "make stack-logs    follow logs; S=worker for one service"
	@echo "make stack-migrate run alembic against the deployment"
	@echo "make stack-cli     admin CLI in the stack, e.g. ARGS=\"users list\""
	@echo "make nginx         install the vhost into the shared front door and reload it"

api:
	uv run python -m api --host $(HOST) --port $(PORT)

api-lan:
	$(MAKE) api HOST=0.0.0.0

worker:
	uv run python -m processing

schema:
	uv run python -m api.schema.stream_export

openapi:
	uv run python -c "import json; from api.main import app; print(json.dumps(app.openapi(), indent=2))" > frontend/openapi.json

gen-client: openapi
	cd frontend && npm run gen

# Dev pairing: `make api` in one terminal, `make web` in another. Vite proxies
# /v1 and /health to API_PROXY (default 127.0.0.1:8000), so the browser talks
# same-origin. Override the bind/port to serve peers, e.g.
#   make web WEB_HOST=10.8.0.1 WEB_PORT=12345 API_PROXY=http://10.8.0.1:8000
WEB_HOST ?= 127.0.0.1
WEB_PORT ?= 5173
API_PROXY ?= http://$(HOST):$(PORT)
web:
	cd frontend && npm install --no-audit --no-fund && API_PROXY=$(API_PROXY) npm run dev -- --host $(WEB_HOST) --port $(WEB_PORT)

web-stack:
	$(MAKE) web API_PROXY=http://10.8.0.1:8098

migrate:
	uv run alembic upgrade head

test:
	uv run pytest tests

test-unit:
	uv run pytest tests/unit

test-e2e:
	uv run pytest tests/e2e


COMPOSE := docker compose -f deploy/docker-compose.yml

sidecars:
	$(COMPOSE) up -d --build asr-parakeet diar-pyannote audio-tagger vision

stack-up:
	$(COMPOSE) up -d --build

stack-down:
	$(COMPOSE) down

stack-build:
	$(COMPOSE) build

stack-ps:
	$(COMPOSE) ps

# S=api, S=worker, ... or leave unset for everything.
stack-logs:
	$(COMPOSE) logs -f $(S)

stack-migrate:
	$(COMPOSE) run --rm migrate

stack-cli:
	$(COMPOSE) run --rm cli python -m cli.main $(ARGS)
