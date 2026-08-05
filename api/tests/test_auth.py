"""Clerk JWT verification.

These sign real RS256 tokens against a throwaway keypair and push them through
`verify_clerk_token`, so the assertions cover the actual signature check rather
than just the presence of the dependency. The forgery cases matter most: a
token that is well-formed but not signed by Clerk must be rejected.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jwt import PyJWK
from jwt.utils import to_base64url_uint

import auth

ISSUER = "https://test-instance.clerk.accounts.dev"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
KID = "test-key-1"


def _keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_from(private_key, kid):
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": to_base64url_uint(numbers.n).decode(),
        "e": to_base64url_uint(numbers.e).decode(),
    }


def _claims(**overrides):
    now = int(time.time())
    base = {
        "sub": f"user_{uuid.uuid4().hex[:12]}",
        "iss": ISSUER,
        "iat": now,
        "exp": now + 60,
    }
    base.update(overrides)
    return base


def _sign(private_key, claims, kid=KID, alg="RS256"):
    return jwt.encode(claims, private_key, algorithm=alg, headers={"kid": kid})


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _verify(token_or_creds):
    """Run the dependency to completion, returning claims or raising."""
    creds = token_or_creds
    if isinstance(token_or_creds, str):
        creds = _creds(token_or_creds)
    return asyncio.run(auth.verify_clerk_token(creds))


@pytest.fixture
def signing_key(monkeypatch):
    """Prime the JWKS cache so no test touches the network."""
    private_key = _keypair()

    monkeypatch.setenv("CLERK_JWKS_URL", JWKS_URL)
    monkeypatch.delenv("CLERK_ISSUER", raising=False)

    monkeypatch.setattr(auth, "_jwks_cache", {KID: PyJWK.from_dict(_jwk_from(private_key, KID))})
    # Recent fetch => an unknown `kid` is rate-limited rather than dialling out.
    monkeypatch.setattr(auth, "_jwks_last_fetch", time.monotonic())
    # A fresh lock per test; one carried across asyncio.run() loops would bind
    # to the first and then reject the second.
    monkeypatch.setattr(auth, "_jwks_lock", asyncio.Lock())

    return private_key


class TestValidToken:
    def test_accepted_and_returns_claims(self, signing_key):
        claims = _claims()
        result = _verify(_sign(signing_key, claims))
        assert result["sub"] == claims["sub"]

    def test_leeway_tolerates_small_clock_skew(self, signing_key):
        # Expired 5s ago; inside the 10s drift allowance.
        result = _verify(_sign(signing_key, _claims(exp=int(time.time()) - 5)))
        assert result["sub"]


class TestRejections:
    def test_missing_credentials(self, signing_key):
        with pytest.raises(HTTPException) as exc:
            _verify(None)
        assert exc.value.status_code == 401

    def test_empty_token(self, signing_key):
        with pytest.raises(HTTPException) as exc:
            _verify(_creds(""))
        assert exc.value.status_code == 401

    def test_garbage_token(self, signing_key):
        with pytest.raises(HTTPException) as exc:
            _verify("not-a-jwt")
        assert exc.value.status_code == 401

    def test_expired_token(self, signing_key):
        with pytest.raises(HTTPException) as exc:
            _verify(_sign(signing_key, _claims(exp=int(time.time()) - 3600)))
        assert exc.value.status_code == 401

    def test_wrong_issuer(self, signing_key):
        with pytest.raises(HTTPException) as exc:
            _verify(_sign(signing_key, _claims(iss="https://evil.example.com")))
        assert exc.value.status_code == 401

    def test_missing_sub_claim(self, signing_key):
        claims = _claims()
        del claims["sub"]
        with pytest.raises(HTTPException) as exc:
            _verify(_sign(signing_key, claims))
        assert exc.value.status_code == 401

    def test_unknown_kid(self, signing_key):
        with pytest.raises(HTTPException) as exc:
            _verify(_sign(signing_key, _claims(), kid="rotated-away"))
        assert exc.value.status_code == 401

    def test_error_detail_is_uniform(self, signing_key):
        """Every rejection reads the same, so probing reveals nothing."""
        with pytest.raises(HTTPException) as exc:
            _verify(_sign(signing_key, _claims(exp=int(time.time()) - 3600)))
        assert exc.value.detail == "Invalid or expired authentication token"


class TestForgery:
    def test_signature_from_a_different_key_is_rejected(self, signing_key):
        """The core check: right `kid`, wrong signer."""
        attacker_key = _keypair()
        token = _sign(attacker_key, _claims(), kid=KID)
        with pytest.raises(HTTPException) as exc:
            _verify(token)
        assert exc.value.status_code == 401

    def test_alg_none_is_rejected(self, signing_key):
        """An unsigned token must never be trusted."""

        def b64(obj):
            raw = json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        token = f"{b64({'alg': 'none', 'kid': KID})}.{b64(_claims())}."
        with pytest.raises(HTTPException) as exc:
            _verify(token)
        assert exc.value.status_code == 401

    def test_hs256_confusion_is_rejected(self, signing_key):
        """Re-signing with the RSA public key as an HMAC secret must fail.

        This is the classic algorithm-confusion attack, and it only fails
        because the allowed algorithm list is pinned to RS256.
        """
        public_pem = signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        # Hand-rolled, because PyJWT's own encode() refuses to use a PEM as an
        # HMAC secret. An attacker has no such scruples, so the token is built
        # the way they would build it.
        def b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        header = b64(json.dumps({"alg": "HS256", "kid": KID}).encode())
        payload = b64(json.dumps(_claims()).encode())
        signing_input = f"{header}.{payload}".encode()
        signature = b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())

        with pytest.raises(HTTPException) as exc:
            _verify(f"{header}.{payload}.{signature}")
        assert exc.value.status_code == 401


class TestConfiguration:
    def test_missing_jwks_url_denies_rather_than_allows(self, monkeypatch, signing_key):
        """Misconfiguration must fail closed, never open."""
        monkeypatch.delenv("CLERK_JWKS_URL", raising=False)
        monkeypatch.setattr(auth, "_jwks_cache", {})
        with pytest.raises(HTTPException) as exc:
            _verify(_sign(signing_key, _claims()))
        assert exc.value.status_code in (500, 401)
        assert exc.value.status_code != 200


class TestJwksUrlNormalization:
    """The bare Clerk origin answers 200 with an empty body, so omitting the
    well-known path fails as an opaque JSON error rather than an obvious
    misconfiguration. Normalize instead of trusting operators to get it right.
    """

    @pytest.mark.parametrize(
        "configured",
        [
            ISSUER,  # bare origin -- the mistake that broke production
            f"{ISSUER}/",  # trailing slash
            JWKS_URL,  # already correct, must stay unchanged
            f'"{JWKS_URL}"',  # pasted with quotes
            f"  {JWKS_URL}  ",  # pasted with whitespace
        ],
    )
    def test_resolves_to_the_jwks_endpoint(self, monkeypatch, configured):
        monkeypatch.setenv("CLERK_JWKS_URL", configured)
        assert auth._jwks_url() == JWKS_URL

    def test_issuer_still_derives_correctly_from_the_bare_origin(self, monkeypatch):
        monkeypatch.setenv("CLERK_JWKS_URL", ISSUER)
        monkeypatch.delenv("CLERK_ISSUER", raising=False)
        assert auth._expected_issuer() == ISSUER

    def test_bare_origin_now_verifies_a_real_token(self, monkeypatch, signing_key):
        """End to end: the misconfiguration should no longer reject users."""
        monkeypatch.setenv("CLERK_JWKS_URL", ISSUER)
        assert _verify(_sign(signing_key, _claims()))["sub"]


# ---------------------------------------------------------------------------
# verify_clerk_or_cron_token: a shared secret, alongside Clerk
# ---------------------------------------------------------------------------


def _verify_cron(token_or_creds):
    creds = token_or_creds
    if isinstance(token_or_creds, str):
        creds = _creds(token_or_creds)
    return asyncio.run(auth.verify_clerk_or_cron_token(creds))


class TestClerkOrCronToken:
    """The endpoint nothing in the browser calls once its button is removed --
    the monthly valuation refresh -- but that still needs a way in for an
    automated caller that cannot complete an interactive Clerk login."""

    def test_the_correct_secret_is_accepted_without_a_clerk_token(self, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "the-real-secret")
        result = _verify_cron("the-real-secret")
        assert result == {"sub": "cron", "cron": True}

    def test_a_valid_clerk_token_still_works_alongside_the_secret(
        self, monkeypatch, signing_key
    ):
        """Removing the button must not lock out a human -- a one-off manual
        re-run, or a future admin trigger, still has a path in."""
        monkeypatch.setenv("CRON_SECRET", "the-real-secret")
        claims = _claims()
        result = _verify_cron(_sign(signing_key, claims))
        assert result["sub"] == claims["sub"]

    def test_a_wrong_secret_falls_through_to_clerk_and_is_rejected(self, monkeypatch):
        """Not a partial match against two schemes -- a wrong guess at the
        secret is just an invalid Clerk token, and gets the same 401."""
        monkeypatch.setenv("CRON_SECRET", "the-real-secret")
        with pytest.raises(HTTPException) as exc:
            _verify_cron("guessed-wrong")
        assert exc.value.status_code == 401

    def test_an_unconfigured_secret_does_not_open_the_endpoint(self, monkeypatch):
        """No CRON_SECRET set must not mean 'any bearer token works' -- a
        deployment that forgot to configure it gets no new way in, rather
        than an endpoint that fails open."""
        monkeypatch.delenv("CRON_SECRET", raising=False)
        with pytest.raises(HTTPException) as exc:
            _verify_cron("anything-at-all")
        assert exc.value.status_code == 401

    def test_an_empty_secret_env_var_does_not_open_the_endpoint(self, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "")
        with pytest.raises(HTTPException) as exc:
            _verify_cron("")
        assert exc.value.status_code == 401

    def test_the_comparison_is_exact_not_a_prefix_match(self, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "the-real-secret")
        with pytest.raises(HTTPException):
            _verify_cron("the-real-secret-extra")
        with pytest.raises(HTTPException):
            _verify_cron("the-real-secre")

    def test_missing_credentials_falls_through_to_the_ordinary_clerk_rejection(
        self, monkeypatch
    ):
        monkeypatch.setenv("CRON_SECRET", "the-real-secret")
        with pytest.raises(HTTPException) as exc:
            _verify_cron(None)
        assert exc.value.status_code == 401
