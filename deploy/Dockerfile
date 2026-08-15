# The application image: API, worker, migrations and CLI all run from it —
# they are the same tree with different entry modules, so one image keeps them
# on a single dependency resolution.
#
# Nothing here needs a compiler: every runtime dependency resolves to a
# cp314/abi3 manylinux wheel in uv.lock (numpy, psycopg[binary], pydantic-core,
# and pysilero-vad, which ships a ggml build rather than onnxruntime). Its only
# system library is libstdc++, already in the trixie base.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Pinned: :latest would make the image unreproducible.
COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /uvx /bin/

WORKDIR /app

# Dependencies resolve from these two files alone (uv.lock records the root as
# a virtual project), so editing source never invalidates this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Copied per package rather than `COPY . .`, which would clobber the venv that
# UV_PROJECT_ENVIRONMENT just put at /app/.venv.
COPY alembic.ini ./
COPY api api
COPY cli cli
COPY database database
COPY processing processing
COPY resources resources
COPY services services
COPY storage storage

# The project has no build backend, so uv installs the dependencies but not the
# project itself and the [project.scripts] entry points never reach PATH.
# Everything runs as `python -m <pkg>` with cwd=/app on sys.path — the same way
# it runs on a developer's machine.
CMD ["python", "-m", "api", "--host", "0.0.0.0", "--port", "8000"]
