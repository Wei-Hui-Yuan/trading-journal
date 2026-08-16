"""What a failing request looks like from the browser's side.

These guard a failure mode that is invisible from the server: Starlette builds
its own 500 response *outside* the CORS middleware, so an unhandled exception
arrives at the browser with no Access-Control-Allow-Origin header. The browser
then refuses to expose the response, and the request fails as a bare "Network
Error" -- pointing every debugging instinct at hosting and DNS while the API is
up and answering.

The fix is only a registration order: the catch-all middleware must be added
*before* CORSMiddleware so that it ends up inside it. Nothing about that reads
as load-bearing, and swapping the two lines back would restore the bug silently
in production while every test still passed. Hence these.
"""

import os

import pytest
from fastapi.testclient import TestClient

ORIGIN = "https://trading-journal-test.vercel.app"

os.environ.setdefault("CORS_ALLOW_ORIGINS", ORIGIN)

import main  # noqa: E402  - must follow the env setup above


@main.app.get("/__test_crash")
async def _crash():
    """A route that fails the way a dead database fails: uncaught."""
    raise RuntimeError("password authentication failed for user")


@pytest.fixture(scope="module")
def client():
    # raise_server_exceptions=False makes the client behave like a real
    # deployment, which returns a 500 rather than re-raising into the caller.
    return TestClient(main.app, raise_server_exceptions=False)


def test_unhandled_error_returns_500(client):
    response = client.get("/__test_crash", headers={"Origin": ORIGIN})
    assert response.status_code == 500


def test_unhandled_error_keeps_cors_header(client):
    """The regression. Without it the browser reports only "Network Error"."""
    response = client.get("/__test_crash", headers={"Origin": ORIGIN})
    assert response.headers.get("access-control-allow-origin") == ORIGIN


def test_unhandled_error_body_is_json_not_plaintext(client):
    """The frontend reads `detail`; Starlette's own 500 is plain text."""
    response = client.get("/__test_crash", headers={"Origin": ORIGIN})
    assert "detail" in response.json()


def test_unhandled_error_does_not_leak_the_exception(client):
    """The message mentioned a password. That must not reach the client."""
    body = client.get("/__test_crash", headers={"Origin": ORIGIN}).text
    assert "password authentication failed" not in body


def test_cors_middleware_is_outermost():
    """States the ordering directly, so a reorder fails with a clear reason."""
    stack = [m.cls.__name__ for m in main.app.user_middleware]
    assert stack[0] == "CORSMiddleware", (
        "CORSMiddleware must stay outermost (added last) or error responses "
        f"lose their CORS headers. Current order: {stack}"
    )


def test_preflights_are_cached_for_longer_than_the_default(client):
    """Every request here is preflighted, and each one is a full round trip.

    Authorization plus a JSON content type are both non-simple headers, so no
    request the app makes qualifies for the simple-request exemption -- the
    browser asks permission for every endpoint URL before sending anything.
    Starlette's 600s default means a tab open for ten minutes pays that again,
    per endpoint, across a link measured at ~218ms each way.

    Asserted as "above the default" rather than as the literal number, because
    the value is a ceiling browsers clamp to their own maximum anyway (Chrome
    7200, Firefox 86400). Pinning the exact figure would fail on a deliberate
    retune; this fails only if the setting is dropped and the default returns.
    """
    response = client.options(
        "/api/round-trips",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    max_age = response.headers.get("access-control-max-age")
    assert max_age is not None, "preflight carried no Access-Control-Max-Age"
    assert int(max_age) > 600, (
        f"preflight cache is {max_age}s -- Starlette's default is 600 and the "
        "explicit max_age has been lost"
    )


def test_health_reports_the_database_failure_class():
    """`unreachable` alone does not say why. The exception class does.

    Named separately because the distinction between a stale password and a
    wrong host is the entire diagnostic value of the endpoint.
    """
    import inspect

    source = inspect.getsource(main.health)
    assert "type(exc).__name__" in source
    assert "database_error" in source


def test_health_does_not_expose_the_connection_password():
    """It parses DATABASE_URL, which contains a live credential."""
    import inspect

    source = inspect.getsource(main.health)
    # The host is taken from the right of rpartition("@"), which discards the
    # userinfo. Returning netloc or the URL itself would publish the password.
    assert "rpartition" in source
    assert "DATABASE_URL," not in source.replace(" ", "")
