"""Clerk JWT verification for protected API routes.

The frontend attaches a Clerk session token as `Authorization: Bearer <jwt>`.
Clerk signs those with RS256 and publishes the matching *public* keys at a
JWKS endpoint, so verifying one is: find the key whose `kid` matches the
token header, check the signature against it, then check the standard time
and issuer claims.

Nothing here talks to Clerk's private API -- JWKS is public, and the only
outbound request is the key fetch, which is cached in-process.

Fails closed. Every path that cannot positively verify a token raises, and
a missing `CLERK_JWKS_URL` is treated as a server fault rather than as
permission to skip the check.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from typing import Any, Optional

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWK
from jwt.exceptions import PyJWTError

logger = logging.getLogger(__name__)

# Pinning the algorithm is what stops `alg: none` and RS256->HS256 confusion,
# where an attacker re-signs a token using the public key as an HMAC secret.
ALGORITHMS = ["RS256"]

# An unrecognized `kid` triggers a refetch so key rotation is picked up without
# a redeploy. This floor keeps that from becoming an outbound-request amplifier:
# without it, junk tokens carrying random `kid`s would each cause a fetch.
JWKS_MIN_REFRESH_SECONDS = 60.0

JWKS_TIMEOUT_SECONDS = 10.0

# Clerk publishes the key set at this path under the frontend-API origin.
JWKS_PATH = "/.well-known/jwks.json"

# Clerk session tokens are short-lived (~60s) and the frontend refreshes them,
# so a little tolerance for clock drift between Northflank and Clerk avoids
# spurious 401s at the boundary.
CLOCK_SKEW_LEEWAY_SECONDS = 10

# `auto_error=False` matters: left at True, a *missing* header short-circuits
# into FastAPI's own 403 "Not authenticated" and never reaches this module, so
# callers would see two different shapes for "unauthenticated". Handling the
# None ourselves keeps every rejection identical.
bearer_scheme = HTTPBearer(auto_error=False)

_jwks_cache: dict[str, PyJWK] = {}
_jwks_last_fetch = 0.0
_jwks_lock = asyncio.Lock()


def _unauthorized() -> HTTPException:
    """Build the single rejection every failure path returns.

    Deliberately uniform: distinguishing "expired" from "bad signature" from
    "unknown key" in the response would confirm details about a token to
    someone probing with guesses.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _jwks_url() -> str:
    url = os.environ.get("CLERK_JWKS_URL", "").strip().strip('"').strip("'")
    if not url:
        # A misconfigured server must not silently accept traffic; 500 so the
        # deployment is visibly broken instead of quietly open.
        logger.error("CLERK_JWKS_URL is not set; refusing to verify tokens")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication is not configured on this server",
        )

    # Accept the bare Clerk frontend-API origin as well as the full URL.
    # The origin answers 200 with an *empty body* rather than 404, so the
    # omission surfaced as a baffling JSON parse error instead of an obvious
    # misconfiguration. Normalizing costs nothing and removes the trap.
    if not url.rstrip("/").endswith(JWKS_PATH):
        url = url.rstrip("/") + JWKS_PATH
    return url


def _expected_issuer() -> str:
    """The `iss` a Clerk token should carry.

    Derived from the JWKS URL by stripping the well-known suffix
    (https://foo.clerk.accounts.dev/.well-known/jwks.json -> https://foo.clerk.accounts.dev),
    since both point at the same Clerk instance. `CLERK_ISSUER` overrides it
    for setups where they differ, e.g. a custom domain.
    """
    override = os.environ.get("CLERK_ISSUER", "").strip()
    if override:
        return override.rstrip("/")
    return _jwks_url().split("/.well-known/")[0].rstrip("/")


async def _refresh_jwks() -> None:
    """Pull the current key set into the cache, rate-limited."""
    global _jwks_last_fetch

    async with _jwks_lock:
        # A concurrent request may have refreshed while we waited on the lock.
        if _jwks_cache and time.monotonic() - _jwks_last_fetch < JWKS_MIN_REFRESH_SECONDS:
            return

        url = _jwks_url()
        try:
            async with httpx.AsyncClient(timeout=JWKS_TIMEOUT_SECONDS) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Could not fetch Clerk JWKS from %s: %s", url, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not reach the authentication service",
            ) from exc

        # Kept separate from the transport failure above: a host that answers
        # but hands back something that isn't a key set is a configuration
        # problem, and calling that "could not reach" sends you hunting for a
        # network fault that does not exist.
        try:
            payload = response.json()
        except ValueError as exc:
            logger.error(
                "Clerk JWKS at %s returned %s that is not JSON (HTTP %s, %d bytes): %s",
                url,
                response.headers.get("content-type", "an unknown type"),
                response.status_code,
                len(response.content),
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service returned an unexpected response",
            ) from exc

        keys: dict[str, PyJWK] = {}
        for entry in payload.get("keys", []):
            kid = entry.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = PyJWK.from_dict(entry)
            except (PyJWTError, KeyError) as exc:
                # One unusable entry shouldn't discard the whole set.
                logger.warning("Skipping unusable JWKS entry %s: %s", kid, exc)

        if not keys:
            logger.error("Clerk JWKS at %s contained no usable keys", url)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not reach the authentication service",
            )

        # Replaced wholesale rather than merged, so keys Clerk has retired stop
        # being accepted here too.
        _jwks_cache.clear()
        _jwks_cache.update(keys)
        _jwks_last_fetch = time.monotonic()


async def _signing_key(kid: str) -> PyJWK:
    key = _jwks_cache.get(kid)
    if key is not None:
        return key

    # Unknown `kid` most often means Clerk rotated keys since the last fetch.
    await _refresh_jwks()

    key = _jwks_cache.get(kid)
    if key is None:
        raise _unauthorized()
    return key


async def verify_clerk_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict[str, Any]:
    """FastAPI dependency: verify the bearer token, or reject the request.

    Returns the decoded claims so a route can read `sub` (the Clerk user id)
    if it needs to scope data per user.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized()

    token = credentials.credentials

    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except PyJWTError as exc:
        # Unparseable header -- not a JWT at all.
        raise _unauthorized() from exc

    if not kid:
        raise _unauthorized()

    key = await _signing_key(kid)

    try:
        return jwt.decode(
            token,
            key.key,
            algorithms=ALGORITHMS,
            issuer=_expected_issuer(),
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
            options={
                # Clerk session tokens carry no `aud` unless a JWT template
                # adds one, so requiring it would reject every valid token.
                "verify_aud": False,
                "require": ["exp", "iat", "sub"],
            },
        )
    except PyJWTError as exc:
        logger.info("Rejected token: %s", exc)
        raise _unauthorized() from exc


async def verify_clerk_or_cron_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict[str, Any]:
    """A Clerk session token, OR the scheduler's shared secret.

    For endpoints nothing in the browser calls anymore once a manual button
    is removed -- the monthly valuation refresh -- but that still needs a way
    in for an automated caller (Northflank's Cron Job) that cannot complete
    an interactive Clerk login.

    The bearer token is checked against CRON_SECRET first, in constant time
    so a wrong guess cannot be timed into a right one. Anything that is not
    an exact match falls through to an ordinary Clerk verification, which is
    what keeps the endpoint reachable from an authenticated browser too --
    a one-off manual re-run, or a future admin trigger, is not locked out by
    building the scheduled path.

    CRON_SECRET is optional. Unset, this behaves exactly like
    `verify_clerk_token` -- a deployment that never configures the secret
    gets no new way in, rather than an endpoint that fails open.
    """
    secret = (os.environ.get("CRON_SECRET") or "").strip()
    if secret and credentials is not None and credentials.credentials:
        if hmac.compare_digest(credentials.credentials, secret):
            return {"sub": "cron", "cron": True}

    return await verify_clerk_token(credentials)
