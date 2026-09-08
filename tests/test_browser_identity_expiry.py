"""Which token gets injected into the headless browser, and its real expiry.

A page that renders logged-out looks like a page that has no content: the
screenshot succeeds, the tree is there, and the only evidence is 401s on the
page's own API calls. These tests pin the two guards against that.

Ported from the same fix in modlix-mcp's `drive.py` / `screenshot.py`
(2026-08-26), adapted: the CFA has no developer-token slot, so an expired
app-user token is reported as an error instead of falling through to anonymous.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from app.agents.appbuilder.tools.modlix.visuals_browser import (
    _jwt_expiry,
    _resolve_identity,
    _usable,
)


def _jwt(**claims) -> str:
    """A JWT with a real payload segment. Signature is never checked."""
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


def _fresh(seconds: int = 3600) -> str:
    return _jwt(exp=int(time.time()) + seconds, sub="u1")


def _expired(seconds: int = 60) -> str:
    return _jwt(exp=int(time.time()) - seconds, sub="u1")


# ── Reading the claim ────────────────────────────────────────────────────


def test_reads_the_exp_claim():
    when = int(time.time()) + 900
    assert _jwt_expiry(_jwt(exp=when)) == when


@pytest.mark.parametrize("token", [
    "", "not-a-jwt", "only.two", "header..signature",
    "header.!!!not-base64!!!.signature",
])
def test_unreadable_tokens_yield_no_expiry(token):
    assert _jwt_expiry(token) is None


def test_a_jwt_without_an_exp_claim_yields_none():
    assert _jwt_expiry(_jwt(sub="u1")) is None


def test_a_non_numeric_exp_yields_none():
    assert _jwt_expiry(_jwt(exp="tomorrow")) is None


# ── Usability ────────────────────────────────────────────────────────────


def test_a_fresh_token_carries_its_own_expiry_not_a_flat_hour():
    token = _fresh(7200)
    result = _usable(token)
    assert result is not None
    assert result[1] == _jwt_expiry(token)


def test_an_expired_token_is_refused():
    assert _usable(_expired()) is None


def test_an_empty_token_is_refused():
    assert _usable("") is None


def test_an_unreadable_token_is_passed_through_with_an_hour():
    """We cannot judge it; refusing outright would be worse than trying."""
    result = _usable("opaque-token")
    assert result is not None
    assert result[0] == "opaque-token"
    assert result[1] == pytest.approx(int(time.time()) + 3600, abs=5)


# ── Identity resolution ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anonymous_short_circuits_everything():
    async def _token():
        raise AssertionError("must not be consulted")
    identity, err = await _resolve_identity(
        {"anonymous": True}, {"get_app_user_token": _token},
    )
    assert (identity, err) == (None, None)


@pytest.mark.asyncio
async def test_a_fresh_app_user_token_is_used_with_its_real_expiry():
    token = _fresh(7200)

    async def _token():
        return token
    identity, err = await _resolve_identity({}, {"get_app_user_token": _token})
    assert err is None
    assert identity == (token, _jwt_expiry(token))


@pytest.mark.asyncio
async def test_an_expired_app_user_token_is_an_error_not_a_silent_anonymous():
    """The whole point: a logged-out render must not be reported as success."""
    async def _token():
        return _expired()
    identity, err = await _resolve_identity({}, {"get_app_user_token": _token})
    assert identity is None
    assert err is not None
    assert "expired" in err.lower()
    assert "anonymous=true" in err, "the error must name the deliberate way out"


@pytest.mark.asyncio
async def test_no_app_user_configured_still_falls_through_to_anonymous():
    """Unconfigured is not the same as expired; a public page must still capture."""
    async def _token():
        raise RuntimeError("no app_user on the request")
    identity, err = await _resolve_identity({}, {"get_app_user_token": _token})
    assert (identity, err) == (None, None)


@pytest.mark.asyncio
async def test_no_identity_source_at_all_is_anonymous():
    identity, err = await _resolve_identity({}, {})
    assert (identity, err) == (None, None)


@pytest.mark.asyncio
async def test_one_shot_login_uses_the_jwt_expiry_when_it_has_one(monkeypatch):
    import app.agents.appbuilder.tools.modlix.visuals_browser as mod
    token = _fresh(7200)

    async def _login(_gateway, _u, _p):
        return token, int(time.time()) + 60, None   # server says 60s, JWT says 2h
    monkeypatch.setattr(mod, "_login_one_shot", _login)

    identity, err = await _resolve_identity(
        {"username": "u", "password": "p"}, {},
    )
    assert err is None
    assert identity == (token, _jwt_expiry(token))


@pytest.mark.asyncio
async def test_one_shot_login_falls_back_to_the_server_expiry(monkeypatch):
    import app.agents.appbuilder.tools.modlix.visuals_browser as mod
    server_exp = int(time.time()) + 1800

    async def _login(_gateway, _u, _p):
        return "opaque-token", server_exp, None
    monkeypatch.setattr(mod, "_login_one_shot", _login)

    identity, err = await _resolve_identity({"username": "u", "password": "p"}, {})
    assert err is None
    assert identity == ("opaque-token", server_exp)


@pytest.mark.asyncio
async def test_a_login_error_is_surfaced(monkeypatch):
    import app.agents.appbuilder.tools.modlix.visuals_browser as mod

    async def _login(_gateway, _u, _p):
        return None, None, "401 Unauthorized"
    monkeypatch.setattr(mod, "_login_one_shot", _login)

    identity, err = await _resolve_identity({"username": "u", "password": "p"}, {})
    assert identity is None
    assert err == "401 Unauthorized"
