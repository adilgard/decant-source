"""d.s Stage 3 — the console logs itself in under local posture.

The requirement: in local posture a human records and types nothing except real
connector credentials. Today the console shows a lock screen and waits for a
pasted token, which is exactly such a step. `/ui/local-session` removes it.

The two properties that matter, and they pull against each other:

  1. In local posture on a loopback bind, the route answers with this box's own
     console credential, so app.js unlocks itself on boot.
  2. In EVERY other case — deployed posture, or a non-loopback bind — the route
     is not registered AT ALL. Not a 403, not an empty 200: absent. A deployed
     console must be byte-for-byte what it was before this build.

Driven through OperatorApp.handle() directly. The route is checked before
_principal() and before anything touches the store, so this needs no Postgres,
no vault, and no live service — which is also the reason it is safe to assert on
so precisely.
"""
from __future__ import annotations

import json

import pytest

from knowledge_hub.config import POSTURE_DEPLOYED, POSTURE_LOCAL, settings
from knowledge_hub.operator_http import (
    LOOPBACK_HOSTS,
    OperatorApp,
    _local_session_provider,
)


class _StubGate:
    def operations(self):
        return {}


class _StubService:
    pass


class _StubResolver:
    """Enough of a CredentialResolver to construct the app. Never reached: the
    local-session route is answered before authentication happens."""

    def resolve_principal(self, credential):
        raise AssertionError("the handoff route must not authenticate")


def _app(local_session=None) -> OperatorApp:
    return OperatorApp(_StubGate(), _StubService(), _StubResolver(),
                       local_session=local_session)


def _get(app: OperatorApp, path: str):
    return app.handle("GET", path, {}, b"")


# ===========================================================================
# The route answers in local posture
# ===========================================================================
def test_the_route_hands_over_a_credential(monkeypatch):
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    status, body = _get(_app(lambda: "kh-operator-default-abc123"), "/ui/local-session")

    assert status == 200
    assert body["credential"] == "kh-operator-default-abc123"
    assert body["posture"] == POSTURE_LOCAL


def test_the_credential_travels_in_the_body_not_the_url(monkeypatch):
    """Never a URL or query string: those land in browser history and in any
    access log that records request lines. A response body does neither."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    status, body = _get(_app(lambda: "secret-token"), "/ui/local-session")
    assert status == 200
    # The handler receives no query string and echoes none back.
    assert "secret-token" not in "/ui/local-session"
    assert body["credential"] == "secret-token"


def test_the_route_is_checked_before_static_file_serving(monkeypatch):
    """Ordering: '/ui/local-session' must not be mistaken for a request for a
    file called 'local-session' in the UI directory (which would 404 and send
    the console to its lock screen for no reason)."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    status, body = _get(_app(lambda: "tok"), "/ui/local-session")
    assert status == 200
    assert isinstance(body, dict), "a static-file answer would be raw bytes"


def _is_handoff(status, body) -> bool:
    """Did this response hand over a credential?"""
    return (status == 200 and isinstance(body, dict)
            and "credential" in body)


def test_a_trailing_slash_is_not_the_handoff_route(monkeypatch):
    """Exact match only. The route is deliberately narrow — it is the one
    endpoint in the service that answers without a credential, so it should be
    reachable by exactly one spelling and no near-misses."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    assert not _is_handoff(*_get(_app(lambda: "tok"), "/ui/local-session/"))
    assert _is_handoff(*_get(_app(lambda: "tok"), "/ui/local-session"))


@pytest.mark.parametrize("near_miss", [
    "/ui/local-session/x", "/local-session", "/v1/local-session",
    "/ui/LOCAL-SESSION", "/ui//local-session",
])
def test_no_near_miss_path_hands_over_a_credential(monkeypatch, near_miss):
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    assert not _is_handoff(*_get(_app(lambda: "tok"), near_miss))


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_only_GET_hands_over_a_credential(monkeypatch, method):
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    app = _app(lambda: "tok")
    status, body = app.handle(method, "/ui/local-session", {}, b"")
    assert not _is_handoff(status, body)


def test_no_session_available_is_a_plain_404(monkeypatch):
    """Posture can move under a running process (reload_settings does that), or
    the store can become unwritable. Either way the console shows its lock
    screen, which is a working fallback rather than an error state."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    status, body = _get(_app(lambda: None), "/ui/local-session")
    assert status == 404
    assert "error" in body


# ===========================================================================
# The route does not exist anywhere else
# ===========================================================================
def test_the_route_is_absent_when_no_provider_was_wired():
    """None means the route was never registered — the request falls through to
    static file handling, exactly as it did before this build. This is what a
    DEPLOYED console does with that path."""
    assert not _is_handoff(*_get(_app(local_session=None),
                                 "/ui/local-session"))


def test_deployed_posture_gets_no_provider(monkeypatch):
    """The load-bearing negative. Deployed posture mints only through the
    print-once ceremony, and that is the point of it."""
    monkeypatch.setattr(settings, "posture", POSTURE_DEPLOYED)
    assert _local_session_provider() is None


def test_local_posture_on_loopback_gets_a_provider(monkeypatch):
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    monkeypatch.setattr(settings, "operator_host", "127.0.0.1")
    assert callable(_local_session_provider())


@pytest.mark.parametrize("host", sorted(LOOPBACK_HOSTS))
def test_every_loopback_spelling_qualifies(monkeypatch, host):
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    monkeypatch.setattr(settings, "operator_host", host)
    assert callable(_local_session_provider())


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.50", "10.0.0.4",
                                  "example.internal", "::"])
def test_a_non_loopback_bind_gets_no_provider(monkeypatch, caplog, host):
    """The condition that makes answering without a credential safe is that the
    packets cannot arrive from off-box. An operator who has deliberately exposed
    the console has changed the threat model, and an unauthenticated credential
    endpoint must not survive that change silently — hence the warning."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    monkeypatch.setattr(settings, "operator_host", host)
    with caplog.at_level("WARNING"):
        assert _local_session_provider() is None
    assert "not loopback" in caplog.text
    assert "local-session" in caplog.text


def test_the_provider_is_a_callable_not_a_captured_token(monkeypatch):
    """Fetched per request so a store rewritten underneath the process — khctl
    minting a second identity, the file deleted and recreated — is picked up
    without a restart."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    tokens = iter(["first", "second"])
    app = _app(lambda: next(tokens))
    assert _get(app, "/ui/local-session")[1]["credential"] == "first"
    assert _get(app, "/ui/local-session")[1]["credential"] == "second"


# ===========================================================================
# It issues a credential; it does not skip authentication
# ===========================================================================
def test_the_handoff_does_not_authenticate_anything(monkeypatch):
    """_StubResolver raises if called. The route hands over a credential and
    stops; the token then goes back through resolve_principal() like any other,
    and every downstream request is gated and audited as before. What is removed
    is a human retyping a secret the process already has on disk."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    status, _ = _get(_app(lambda: "tok"), "/ui/local-session")
    assert status == 200  # would have raised if it touched the resolver


def test_other_endpoints_still_require_a_credential(monkeypatch):
    """The handoff must not have opened a hole anywhere else: an unauthenticated
    request to a real endpoint is still refused, in local posture too."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)

    class _Refusing:
        def resolve_principal(self, credential):
            from knowledge_hub.choke_point import PrincipalUnresolvable
            raise PrincipalUnresolvable("no")

    app = OperatorApp(_StubGate(), _StubService(), _Refusing(),
                      local_session=lambda: "tok")
    status, body = app.handle("GET", "/v1/actions", {}, b"")
    assert status == 401
    assert body == {"error": "unauthorized"}


# ===========================================================================
# Health says what it actually checked
# ===========================================================================
def test_health_names_the_credential_store_and_posture(monkeypatch):
    """Found by reading live output, not by a test: in local posture health
    answered `"vault": true` when the answer came from a JSON file. The two
    vault fields keep their names and meanings — app.js branches on them, a
    deployed monitor may scrape them — so the fix is additive: say WHAT was
    checked beside the verdict."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)

    class _FileResolver:
        def status(self):
            return "ok"

    class _PingablePostgres:
        def ping_postgres(self):
            return True

    app = OperatorApp(_StubGate(), _PingablePostgres(), _FileResolver())
    status, body = app.handle("GET", "/v1/health", {}, b"")

    assert status == 200
    assert body["credential_store"] == "_FileResolver"
    assert body["posture"] == POSTURE_LOCAL
    # The existing contract is untouched.
    assert body["vault"] is True and body["vault_status"] == "ok"


def test_health_still_reports_a_sealed_vault_distinctly(monkeypatch):
    """F1 preserved: sealed is not unreachable is not ok, and a sealed vault
    still reads as NOT usable. "sealed" cannot occur for a file, but the
    vocabulary is shared so the health surfaces branch identically."""
    monkeypatch.setattr(settings, "posture", POSTURE_DEPLOYED)

    class _SealedResolver:
        def status(self):
            return "sealed"

    class _PingablePostgres:
        def ping_postgres(self):
            return True

    app = OperatorApp(_StubGate(), _PingablePostgres(), _SealedResolver())
    status, body = app.handle("GET", "/v1/health", {}, b"")

    assert status == 503
    assert body["vault"] is False
    assert body["vault_status"] == "sealed"
    assert body["posture"] == POSTURE_DEPLOYED


# ===========================================================================
# The browser side
# ===========================================================================
def _app_js() -> str:
    from pathlib import Path

    import knowledge_hub
    return (Path(knowledge_hub.__file__).parent / "operator_ui" / "app.js"
            ).read_text(encoding="utf-8")


def test_the_ui_tries_the_handoff_before_showing_the_lock_screen():
    js = _app_js()
    assert "/ui/local-session" in js
    assert "bootAuth" in js
    saved = js.index("sessionStorage.getItem(TOKEN_KEY)")
    handoff = js.index("/ui/local-session")
    assert saved < handoff, (
        "a token this tab already holds must be tried first — re-minting on "
        "every reload would grow the registry for no reason")


def test_the_ui_has_no_posture_logic_of_its_own():
    """The server's answer is the whole decision. No posture branch in the
    browser means nothing here can weaken a deployed console, which simply
    never answers the route."""
    js = _app_js()
    for token in ("KH_POSTURE", "posture ===", "isLocal", "is_local"):
        assert token not in js


def test_the_ui_still_falls_back_to_the_lock_screen():
    js = _app_js()
    boot = js[js.index("async function bootAuth"):]
    assert "token-input" in boot, "the lock screen must remain the fallback"
    assert "catch" in boot, "a 404 or offline service must not throw"
