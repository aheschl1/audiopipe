"""Containers, environment, server process and seeding for the e2e suite.

Every test runs against a throwaway Postgres and MinIO started for the session,
with the API served by a real ``python -m api`` process on a loopback port.
"""

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import boto3
import httpx
import psycopg
import pytest
import pytest_asyncio
from testcontainers.community.minio import MinioContainer
from testcontainers.community.postgres import PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[2]

# Not the production bucket name: even a misconfigured endpoint cannot then
# collide with real data.
TEST_BUCKET = "test-audio"

TABLES = (
    "users, recording_sessions, resource_segments, auth_tokens, "
    "processing_jobs, pipeline_artifacts, speakers, cluster_params, "
    "speaker_pins, ab_votes"
)

SERVER_BOOT_TIMEOUT = 30.0


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    # The pgvector image is the official postgres:16 plus the vector
    # extension, which migration 0006 requires — mirrors production.
    with PostgresContainer("pgvector/pgvector:pg16") as container:
        yield container


@pytest.fixture(scope="session")
def minio() -> Iterator[MinioContainer]:
    with MinioContainer("minio/minio") as container:
        yield container


@pytest.fixture(scope="session")
def stub_audio_services() -> Iterator[str]:
    """Fake transcription + diarization services; yields the base URL (with /v1)."""
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.e2e.stub_audio_services", str(port)], cwd=REPO_ROOT
    )
    base_url = f"http://127.0.0.1:{port}/v1"
    deadline = time.monotonic() + SERVER_BOOT_TIMEOUT
    try:
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "stub transcriber did not start"
            time.sleep(0.1)
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.fixture(scope="session")
def test_env(
    postgres: PostgresContainer, minio: MinioContainer, stub_audio_services: str
) -> dict[str, str]:
    """Point every DATABASE_*/STORAGE_* variable at the containers.

    Both settings objects are ``@lru_cache``d, so the caches are cleared after
    the environment is set. Environment variables outrank the repo's .env file,
    and every variable is specified here so none can fall through to it.
    """
    config = minio.get_config()
    env = {
        "DATABASE_HOST": postgres.get_container_host_ip(),
        "DATABASE_PORT": str(postgres.get_exposed_port(5432)),
        "DATABASE_NAME": postgres.dbname,
        "DATABASE_USER": postgres.username,
        "DATABASE_PASSWORD": postgres.password,
        "DATABASE_POOL_MIN_SIZE": "1",
        "DATABASE_POOL_MAX_SIZE": "5",
        "DATABASE_CONNECT_TIMEOUT": "10",
        "STORAGE_ENDPOINT_URL": f"http://{config['endpoint']}",
        "STORAGE_ACCESS_KEY": config["access_key"],
        "STORAGE_SECRET_KEY": config["secret_key"],
        "STORAGE_BUCKET": TEST_BUCKET,
        "STORAGE_REGION": "us-east-1",
        "STORAGE_PRESIGN_EXPIRY_SECONDS": "3600",
        # Small streaming windows so websocket tests see acks quickly, and a
        # chunk cap small enough to exercise without allocating megabytes.
        "API_STREAM_MAX_CHUNK_BYTES": "262144",
        "API_STREAM_ACK_WINDOW_CHUNKS": "5",
        "API_STREAM_ACK_WINDOW_SECONDS": "0.5",
        "API_STREAM_HELLO_TIMEOUT_SECONDS": "5",
        "API_STREAM_IDLE_TIMEOUT_SECONDS": "30",
        "API_STREAM_INGEST_CONCURRENCY": "4",
        # Fast enough that stream tests observe an external end/split without
        # waiting out the production 5s check.
        "API_STREAM_SESSION_CHECK_SECONDS": "0.2",
        # v2 pooling small enough that a handful of 50ms frames spans several
        # stored segments, with a latency flush observable within a test.
        "API_STREAM_MAX_AUDIO_FRAME_BYTES": "65536",
        "API_STREAM_POOL_TARGET_BYTES": "4096",
        "API_STREAM_POOL_MAX_BUFFER_BYTES": "1048576",
        "API_STREAM_POOL_MAX_LATENCY_SECONDS": "0.3",
        "API_STREAM_POOL_FLUSH_RETRIES": "2",
        "API_STREAM_POOL_RETRY_BACKOFF_SECONDS": "0.05",
        "API_SESSION_STALE_SECONDS": "300",
        # Effectively parks the server's sweeper so test_stream can drive
        # end_stale deterministically.
        "API_SESSION_SWEEP_INTERVAL_SECONDS": "3600",
        # Fast worker cadence and tiny retry windows so processing tests
        # observe discovery, retries, and death within seconds.
        "PROCESSING_POLL_INTERVAL_SECONDS": "0.2",
        "PROCESSING_MAX_ATTEMPTS": "2",
        "PROCESSING_RETRY_BACKOFF_BASE_SECONDS": "0.05",
        "PROCESSING_RETRY_BACKOFF_CAP_SECONDS": "0.2",
        "PROCESSING_SHUTDOWN_GRACE_SECONDS": "5",
        # Deterministic speech detection (a sine tone IS activity) and stub
        # model services instead of GPU containers. The stub's canned turns
        # are 150ms, so the min-turn filter is lowered below them.
        "PROCESSING_VAD_BACKEND": "energy",
        "PROCESSING_TRANSCRIBER_BASE_URL": stub_audio_services,
        "PROCESSING_TRANSCRIBER_PROTOCOL": "openai",
        "PROCESSING_TRANSCRIBER_TIMEOUT_SECONDS": "10",
        "PROCESSING_DIARIZER_BASE_URL": stub_audio_services,
        "PROCESSING_DIARIZER_TIMEOUT_SECONDS": "10",
        "PROCESSING_CLASSIFIER_BASE_URL": stub_audio_services,
        "PROCESSING_CLASSIFIER_TIMEOUT_SECONDS": "10",
        # The API embeds search queries through the same stub.
        "API_CLASSIFIER_BASE_URL": stub_audio_services,
        "API_CLASSIFIER_TIMEOUT_SECONDS": "10",
        "PROCESSING_DIARIZE_MIN_TURN_MS": "100",
        # The stub's turns are 150ms; the default 3s gate would silently
        # skip every embedding and no cluster test could pass.
        "PROCESSING_CLUSTER_MIN_TALK_MS": "100",
        # The split-scenario stub turns carry 200-250ms of clean audio each;
        # the real-world floors (1s vote, 2s per sub-group) would veto every
        # split before the purity audit could be exercised.
        "PROCESSING_DIARIZE_TURN_MIN_CLEAN_MS": "100",
        "PROCESSING_DIARIZE_SPLIT_MIN_CLEAN_MS": "200",
        # The stub tagger answers exactly two windows and the seeded grids
        # are just as small; the production thresholds are calibrated for
        # real-length audio and would drop every stub-scale span.
        "PROCESSING_SOUND_SPAN_ENTER_SCORE": "0.35",
        "PROCESSING_SOUND_SPAN_SUSTAIN_SCORE": "0.20",
        "PROCESSING_SOUND_SPAN_MIN_WINDOWS": "2",
    }
    os.environ.update(env)

    from api.config import get_settings as api_settings
    from database.config import get_settings as db_settings
    from live.config import get_settings as live_settings
    from processing.config import get_settings as processing_settings
    from storage.config import get_settings as storage_settings

    api_settings.cache_clear()
    db_settings.cache_clear()
    live_settings.cache_clear()
    processing_settings.cache_clear()
    storage_settings.cache_clear()
    return env


@pytest.fixture(scope="session", autouse=True)
def settings_guard(test_env: dict[str, str]) -> None:
    """Refuse to run if the application is not pointed at the containers.

    The per-test cleanup truncates tables and empties a bucket. This asserts
    those operations cannot reach anything but the throwaway containers.
    """
    from database.config import get_settings as db_settings
    from storage.config import get_settings as storage_settings

    database = db_settings()
    storage = storage_settings()

    assert str(database.port) == test_env["DATABASE_PORT"], "database is not the test container"
    assert database.name == test_env["DATABASE_NAME"]
    assert storage.endpoint_url == test_env["STORAGE_ENDPOINT_URL"], "storage is not the container"
    assert storage.bucket == TEST_BUCKET != "audio", "storage bucket is not the test bucket"


@pytest.fixture(scope="session")
def s3(test_env: dict[str, str], settings_guard: None) -> Any:
    client = boto3.client(
        "s3",
        endpoint_url=test_env["STORAGE_ENDPOINT_URL"],
        aws_access_key_id=test_env["STORAGE_ACCESS_KEY"],
        aws_secret_access_key=test_env["STORAGE_SECRET_KEY"],
        region_name=test_env["STORAGE_REGION"],
    )
    client.create_bucket(Bucket=TEST_BUCKET)
    return client


@pytest.fixture(scope="session")
def migrated(test_env: dict[str, str], settings_guard: None) -> None:
    """Apply Alembic migrations to the throwaway database.

    A subprocess rather than ``alembic.command``: it keeps Alembic's global
    configuration out of the pytest process.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, **test_env},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def server(test_env: dict[str, str], migrated: None, s3: Any) -> Iterator[str]:
    """Run the API as a real process and yield its base URL."""
    port = _free_port()
    log = open(REPO_ROOT / ".pytest-api.log", "w+")
    process = subprocess.Popen(
        [sys.executable, "-m", "api", "--port", str(port), "--log-level", "warning"],
        cwd=REPO_ROOT,
        env={**os.environ, **test_env},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + SERVER_BOOT_TIMEOUT
    try:
        while True:
            if process.poll() is not None:
                log.seek(0)
                raise AssertionError(f"api exited with {process.returncode}:\n{log.read()}")
            try:
                if httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                log.seek(0)
                raise AssertionError(f"api did not start in {SERVER_BOOT_TIMEOUT}s:\n{log.read()}")
            time.sleep(0.2)
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()


@pytest.fixture(scope="session")
def worker(test_env: dict[str, str], migrated: None, s3: Any) -> Iterator[None]:
    """Run the processing supervisor as a real process.

    Readiness has no HTTP endpoint to poll, so it is the per-pipeline
    ``worker <name> ready`` log lines instead.
    """
    from processing.registry import names

    log = open(REPO_ROOT / ".pytest-worker.log", "w+")
    process = subprocess.Popen(
        [sys.executable, "-m", "processing", "--log-level", "info"],
        cwd=REPO_ROOT,
        env={**os.environ, **test_env},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + SERVER_BOOT_TIMEOUT
    try:
        while True:
            if process.poll() is not None:
                log.seek(0)
                raise AssertionError(f"worker exited with {process.returncode}:\n{log.read()}")
            log.seek(0)
            content = log.read()
            if all(f"worker {name} ready" in content for name in names()):
                break
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"worker did not start in {SERVER_BOOT_TIMEOUT}s:\n{content}"
                )
            time.sleep(0.2)
        yield
    finally:
        process.terminate()
        process.wait(timeout=15)
        log.close()


async def wait_for_job(session_id, pipeline: str, status: str, timeout: float = 15.0):
    """Poll until the session has a `pipeline` job in `status`."""
    import asyncio

    from database.pipe import DatabasePipe

    jobs: list = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with DatabasePipe() as pipe:
            jobs = await pipe.jobs.list_for_session(session_id)
        for job in jobs:
            if job.pipeline == pipeline and job.status == status:
                return job
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"no {pipeline} job reached {status!r} for session {session_id} "
        f"within {timeout}s; jobs: {[(j.pipeline, j.status) for j in jobs]}"
    )


@pytest_asyncio.fixture(autouse=True)
async def clean_state(test_env: dict[str, str], migrated: None, s3: Any) -> Any:
    """Empty both stores before each test, and drop the pooled connections after.

    Cleanup uses blocking psycopg/boto3 clients, which touch no event loop. The
    teardown must run inside the test's own loop: ``database.pipe`` caches its
    pool there, and closing it from any other loop fails.
    """
    _truncate(test_env)
    _empty_bucket(s3)
    yield
    from database.pipe import close_pool
    from storage.pipe import close_blob_client

    await close_blob_client()
    await close_pool()


def _truncate(env: dict[str, str], attempts: int = 10) -> None:
    """Empty the tables, waiting out any worker mid-transaction.

    TRUNCATE takes ACCESS EXCLUSIVE on every table at once while the live
    workers hold locks on a subset in their own order, so it can deadlock or
    block. `lock_timeout` turns that into a fast, retryable error rather than
    a hang; the workers' transactions are short, so a retry gets through.
    """
    for attempt in range(attempts):
        try:
            with psycopg.connect(
                host=env["DATABASE_HOST"],
                port=int(env["DATABASE_PORT"]),
                dbname=env["DATABASE_NAME"],
                user=env["DATABASE_USER"],
                password=env["DATABASE_PASSWORD"],
                autocommit=True,
            ) as conn:
                conn.execute("SET lock_timeout = '2s'")
                conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
            return
        except (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable):
            if attempt == attempts - 1:
                raise
            time.sleep(0.25)


def _empty_bucket(client: Any) -> None:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=TEST_BUCKET):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=TEST_BUCKET, Delete={"Objects": keys})


@dataclass
class Account:
    """A seeded user plus a usable API token."""

    user: Any
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest_asyncio.fixture
async def account(clean_state: None) -> Account:
    return await make_account()


@pytest_asyncio.fixture
async def other_account(clean_state: None) -> Account:
    return await make_account()


async def make_account(password: str = "s3cret") -> Account:
    """Seed a user and token through the production code paths."""
    from database.pipe import DatabasePipe
    from database.schema.users import UserCreate

    async with DatabasePipe() as pipe:
        user = await pipe.users.create(
            UserCreate(email=f"{uuid4().hex[:12]}@example.com", password=password)
        )
        issued = await pipe.tokens.issue(user.id, name="e2e")
    return Account(user=user, token=issued.token.get_secret_value())


async def make_session(account: Account, device: str = "glasses-01", label: str | None = None):
    from database.pipe import DatabasePipe
    from database.schema.sessions import SessionCreate

    async with DatabasePipe() as pipe:
        return await pipe.sessions.create(
            SessionCreate(user_id=account.user.id, device=device, label=label)
        )


async def ingest(session_id, payload: bytes, **kwargs):
    from services import segments as segment_service

    return await segment_service.ingest_segment(
        session_id, payload, content_type="audio/wav", filename="clip.wav", **kwargs
    )


async def ingest_location(session_id, points: list[dict], **kwargs):
    import json

    from services import segments as segment_service

    return await segment_service.ingest_segment(
        session_id, json.dumps({"points": points}).encode(), resource="location", **kwargs
    )


@pytest_asyncio.fixture
async def client(server: str) -> Any:
    async with httpx.AsyncClient(base_url=server, timeout=30.0) as http:
        yield http
