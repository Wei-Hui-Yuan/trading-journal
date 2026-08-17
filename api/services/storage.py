"""Supabase Storage, over its REST API.

The chart screenshot attached to a plan is the one thing this journal stores
that is not a number to be queried. It is never joined, filtered, aggregated
or windowed -- it is fetched whole, by key, when someone opens the plan it
belongs to. Postgres would hold it, but the free tier gives 500 MB of
database against 1 GB of object storage, and the database allowance is the
one every queryable table competes for. So the bytes go to Storage and
`planned_trades` keeps the key (migration 025).

Talks to the REST API with httpx rather than pulling in supabase-py: three
calls are needed (put, get, delete), httpx is already a dependency, and the
alternative brings a client library plus its own transport stack to wrap the
same three HTTP requests.

The service role key is used, not the anon key, and only ever from here --
server side. That is deliberate: it bypasses row-level security, so the
bucket stays private with no policies to design, and the endpoints in main.py
are already behind `verify_clerk_token`. A browser never sees this key and
never talks to Storage directly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Where plan charts live. A bucket rather than a folder in an existing one so
# its access policy, and any future retention rule, applies to exactly this
# kind of object.
BUCKET = os.environ.get("SUPABASE_CHART_BUCKET", "plan-charts")

# Storage is a separate service from Postgres and can be slow on a cold
# bucket. Longer than a page load should wait, short enough that a hung
# request cannot occupy a worker until gunicorn's 120s ceiling.
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class StorageError(RuntimeError):
    """A Storage call failed, or the credentials to make one are missing.

    Carries `status` so the endpoint can distinguish "you have not configured
    this yet" (503, and actionable) from "the object is not there" (404) and
    from an upstream fault (502), rather than collapsing all three into 500.
    """

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class StoredObject:
    """What came back out of the bucket."""

    data: bytes
    content_type: str


def _config() -> tuple[str, str]:
    """The base URL and service key, or a 503 explaining what to set.

    Read per call rather than captured at import: the module is imported
    while the app boots, and binding missing credentials once would make a
    correctly-configured restart still report them missing.
    """
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        missing = [
            name
            for name, value in (
                ("SUPABASE_URL", url),
                ("SUPABASE_SERVICE_ROLE_KEY", key),
            )
            if not value
        ]
        raise StorageError(
            "Chart storage is not configured on the server: "
            f"{', '.join(missing)} is not set. Add it to api/.env (the file is "
            "gitignored) and create the "
            f"'{BUCKET}' bucket in the Supabase dashboard.",
            status=503,
        )
    return url, key


def is_configured() -> bool:
    """Whether an upload could succeed, without attempting one."""
    try:
        _config()
    except StorageError:
        return False
    return True


def _object_url(base: str, path: str) -> str:
    return f"{base}/storage/v1/object/{BUCKET}/{path}"


# One client for the process rather than one per call.
#
# All three functions below used to build an AsyncClient and tear it down again,
# so every chart byte paid a full DNS + TCP + TLS handshake to Supabase Storage
# before anything moved. This deployment makes that expensive twice over: the API
# runs in us-central1 while the bucket is in ap-southeast-1, so the handshake is
# several round trips across the Pacific -- and the image then crosses it a
# second time reaching a browser that is also in Singapore.
#
# The same reasoning `_warm_connection_pool` applies to Postgres in main.py, for
# the same reason, against the same latency.
_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    """The shared client, built on first use.

    Lazily rather than at import. This module is imported while the app boots,
    and a client constructed there would bind to whatever event loop happened to
    be current then rather than the one that ends up serving requests.

    Rebuilt if it has been closed, so a shutdown followed by more work -- which
    is the shape of a test suite, not of production -- gets a working client
    instead of a ClosedError.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=TIMEOUT)
    return _client


async def aclose() -> None:
    """Release the shared client. Called from the app's lifespan shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def upload(path: str, data: bytes, content_type: str) -> None:
    """Write bytes to `path`, replacing whatever was there.

    Upsert rather than create: re-uploading a chart for a plan that already
    has one is an ordinary edit ("that was the wrong screenshot"), and the
    path is derived from the plan id so it is the same key every time. Without
    upsert the second attempt would 409 and the user would have to delete
    first to correct a mistake.
    """
    base, key = _config()
    try:
        response = await _http().post(
            _object_url(base, path),
            content=data,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": content_type,
                "x-upsert": "true",
                "Cache-Control": "max-age=31536000",
            },
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"Could not reach Supabase Storage: {exc}") from exc

    if response.status_code == 404:
        # The bucket itself, not the object -- an upload cannot 404 on a key
        # it is creating.
        raise StorageError(
            f"The '{BUCKET}' bucket does not exist. Create it in the Supabase "
            "dashboard under Storage, then retry.",
            status=503,
        )
    if response.status_code >= 400:
        logger.error(
            "Storage upload failed for %s: %s %s",
            path, response.status_code, response.text[:500],
        )
        raise StorageError(
            f"Storage rejected the upload ({response.status_code}).",
        )


async def download(path: str) -> StoredObject:
    """Read the object back, for the API to serve to the browser."""
    base, key = _config()
    try:
        response = await _http().get(
            _object_url(base, path),
            headers={"Authorization": f"Bearer {key}"},
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"Could not reach Supabase Storage: {exc}") from exc

    if response.status_code == 404:
        raise StorageError("That chart is no longer in storage.", status=404)
    if response.status_code >= 400:
        logger.error(
            "Storage download failed for %s: %s %s",
            path, response.status_code, response.text[:500],
        )
        raise StorageError(f"Storage refused the read ({response.status_code}).")

    return StoredObject(
        data=response.content,
        content_type=response.headers.get("content-type", "application/octet-stream"),
    )


async def delete(path: str) -> None:
    """Remove the object.

    A 404 is success, not failure: the caller's goal is that the object is
    gone, and it already is. Treating it as an error would leave a plan
    permanently undeletable whenever a previous attempt got halfway.
    """
    base, key = _config()
    try:
        response = await _http().delete(
            _object_url(base, path),
            headers={"Authorization": f"Bearer {key}"},
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"Could not reach Supabase Storage: {exc}") from exc

    if response.status_code == 404:
        logger.info("Storage delete: %s was already gone.", path)
        return
    if response.status_code >= 400:
        logger.error(
            "Storage delete failed for %s: %s %s",
            path, response.status_code, response.text[:500],
        )
        raise StorageError(f"Storage refused the delete ({response.status_code}).")
