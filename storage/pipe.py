"""The entry point into blob storage, the counterpart of ``DatabasePipe``.

    async with BlobPipe() as blobs:
        info = await blobs.put(key, data, content_type="audio/wav")
        url = await blobs.presign_get(key)

Object writes are durable as soon as they land; there is no transaction to roll
back. :mod:`services.segments` cleans up objects whose database row failed.

The S3 client is process-wide and opened on first use, the counterpart of
``database.pipe.get_pool``: building a client resolves credentials and creates
a fresh connection pool, so a client per operation would redo the TCP/TLS
handshake on every blob touch. ``BlobPipe`` only borrows it.
"""

import asyncio
from types import TracebackType
from typing import Any

import aioboto3
from botocore.config import Config

from storage.config import StorageSettings, get_settings
from storage.s3 import S3BlobStore

_session = aioboto3.Session()

# Signature v4 explicitly: against a custom endpoint botocore can otherwise fall
# back to the v2 presigning scheme, which AWS S3 no longer accepts in regions
# created after 2014 -- so presigned URLs would break on a move off MinIO.
_CONFIG = Config(signature_version="s3v4")

_client: Any = None
_client_cm: Any = None
_client_loop: asyncio.AbstractEventLoop | None = None
_lock_obj: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _make_client_cm(settings: StorageSettings) -> Any:
    return _session.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        region_name=settings.region,
        config=_CONFIG,
    )


def _lock() -> asyncio.Lock:
    """The creation lock for the running loop (see ``database.pipe._lock``)."""
    global _lock_obj, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock_obj is None or _lock_loop is not loop:
        _lock_obj = asyncio.Lock()
        _lock_loop = loop
    return _lock_obj


async def get_blob_client(settings: StorageSettings | None = None) -> Any:
    """The process-wide S3 client, opened on first use.

    Bound to the running loop (aiohttp connections are): a client left over
    from a previous loop -- a test suite runs one loop per test -- is dropped
    and rebuilt, since its connections died with that loop.
    """
    global _client, _client_cm, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        async with _lock():
            if _client is None or _client_loop is not loop:
                _client = _client_cm = None  # a stale client cannot be closed from here
                cm = _make_client_cm(settings or get_settings())
                _client = await cm.__aenter__()
                _client_cm = cm
                _client_loop = loop
    return _client


async def close_blob_client() -> None:
    """Close the shared client. Call once on application shutdown."""
    global _client, _client_cm, _client_loop, _lock_obj, _lock_loop
    async with _lock():
        cm, on_this_loop = _client_cm, _client_loop is asyncio.get_running_loop()
        _client = _client_cm = None
        if cm is not None and on_this_loop:
            await cm.__aexit__(None, None, None)
    _client_loop = None
    _lock_obj = _lock_loop = None


class BlobPipe:
    """Borrows the shared client and exposes the blob store over it.

    Passing explicit ``settings`` opens a private client for the block instead
    (a test pointing somewhere else), closed on exit -- the analogue of
    ``DatabasePipe(pool=...)``.
    """

    def __init__(self, *, settings: StorageSettings | None = None) -> None:
        self._settings = settings
        self._cm = None

    async def __aenter__(self) -> S3BlobStore:
        if self._settings is not None:
            settings = self._settings
            self._cm = _make_client_cm(settings)
            client = await self._cm.__aenter__()
        else:
            settings = get_settings()
            client = await get_blob_client(settings)
        return S3BlobStore(client, settings.bucket, settings.presign_expiry_seconds)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        cm, self._cm = self._cm, None
        if cm is None:
            return None
        return await cm.__aexit__(exc_type, exc, tb)
