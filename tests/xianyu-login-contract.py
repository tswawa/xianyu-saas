#!/usr/bin/env python3
"""Offline contracts for the server-side Xianyu QR login manager."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.cookiejar import Cookie


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import xianyu_login


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class Response:
    def __init__(self, url, payload, status=200):
        self.url = url
        self.status_code = status
        self.headers = {"Content-Length": str(len(json.dumps(payload)))}
        self.content = json.dumps(payload).encode()
        self._payload = payload
        self.closed = False

    def iter_content(self, chunk_size, decode_unicode=False):
        assert chunk_size == 16 * 1024
        assert decode_unicode is False
        for offset in range(0, len(self.content), 17):
            yield self.content[offset : offset + 17]

    def close(self):
        self.closed = True


def cookie(name, value, domain=".goofish.com", path="/"):
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None},
        rfc2109=False,
    )


class Jar:
    def __init__(self):
        self.values = []

    def __iter__(self):
        return iter(self.values)

    def clear(self):
        self.values.clear()


class FakeSession:
    def __init__(self, query=None, blocker=None):
        self.cookies = Jar()
        self.query = list(query or ["NEW"])
        self.blocker = blocker
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] <= 15
        assert kwargs["stream"] is True
        self.calls.append((method, url, kwargs))
        if url == xianyu_login.GENERATE_URL:
            return Response(
                url,
                {
                    "content": {
                        "success": True,
                        "data": {
                            "codeContent": (
                                "https://passport.goofish.com/qrcodeCheck.htm?k=secret"
                            ),
                            "t": 1700000000000,
                            "ck": "query-check-contract-1234567890",
                        },
                    }
                },
            )
        if url == xianyu_login.QUERY_URL:
            assert method == "POST"
            assert kwargs["data"]["t"] == "1700000000000"
            assert kwargs["data"]["ck"] == "query-check-contract-1234567890"
            if self.blocker:
                self.blocker.wait(timeout=2)
            status = self.query.pop(0) if self.query else "NEW"
            data = {"qrCodeStatus": status}
            if status == "CONFIRMED":
                data["bizExt"] = json.dumps({"loginToken": "login-token-contract"})
            return Response(url, {"content": {"success": True, "data": data}})
        if url == xianyu_login.LOGIN_URL:
            assert method == "POST"
            assert kwargs["params"]["token"] == "login-token-contract"
            self.cookies.values.extend(
                [
                    cookie("unb", "123456"),
                    cookie("cookie2", "session-contract"),
                    cookie("evil", "must-not-leak", ".evil.example"),
                    cookie("wrongpath", "must-not-leak", ".goofish.com", "/private"),
                ]
            )
            return Response(url, {"content": {"success": True, "data": {}}})
        if url == xianyu_login.MTOP_URL:
            assert method == "POST"
            self.cookies.values.append(cookie("_m_h5_tk", "mtop-token_contract"))
            return Response(url, {"ret": ["FAIL_SYS_TOKEN_EXPIRED::令牌过期"], "data": {}})
        raise AssertionError("unexpected URL")

    def close(self):
        self.closed = True


def svg_factory(_value):
    return b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h10v10z"/></svg>'


def expect_code(code, callback):
    try:
        callback()
    except xianyu_login.XianyuLoginError as error:
        assert error.code == code, (error.code, code)
        assert "secret" not in error.message
    else:
        raise AssertionError(f"expected {code}")


def manager(clock, sessions, capacity=4, cooldown=5, upstream_gate=None):
    return xianyu_login.XianyuLoginManager(
        session_factory=lambda: sessions.pop(0),
        qr_factory=svg_factory,
        clock=clock,
        ttl_seconds=150,
        capacity=capacity,
        cooldown_seconds=cooldown,
        request_timeout=3,
        poll_interval_seconds=0,
        upstream_gate=upstream_gate,
        sweep_interval_seconds=0.01,
    )


def main():
    clock = Clock()
    first_session = FakeSession(["NEW", "SCANED", "CONFIRMED"])
    logins = manager(clock, [first_session])
    started = logins.start(10)
    assert set(started) == {"login_id", "status", "expires_in"}
    assert started["status"] == "waiting" and started["expires_in"] == 150
    assert "secret" not in repr(started)
    login_id = started["login_id"]
    assert b"<script" not in logins.qr_svg(10, login_id).lower()

    expect_code("login_not_found", lambda: logins.poll(11, login_id))
    expect_code("login_not_found", lambda: logins.qr_svg(11, login_id))
    expect_code("login_exists", lambda: logins.start(10))
    waiting = logins.poll(10, login_id)
    scanned = logins.poll(10, login_id)
    assert waiting["status"] == "waiting"
    assert scanned["status"] == "scanned"
    assert "cookie" not in repr(scanned).lower()
    confirmed = logins.poll(10, login_id)
    assert confirmed["status"] == "confirmed"
    assert set(confirmed) == {"login_id", "status", "expires_in"}
    header = logins.begin_consume(10, login_id)
    assert "unb=123456" in header and "_m_h5_tk=mtop-token_contract" in header
    assert "evil" not in header and "wrongpath" not in header
    expect_code("login_busy", lambda: logins.begin_consume(10, login_id))
    logins.finish_consume(10, login_id, False)
    assert logins.begin_consume(10, login_id) == header
    logins.finish_consume(10, login_id, True)
    expect_code("login_not_found", lambda: logins.begin_consume(10, login_id))
    assert list(first_session.cookies) == []
    assert first_session.closed is True

    # Current passport variants may finish through Set-Cookie without a
    # separate continuation token. The same strict Cookie checks still apply.
    class CookieConfirmedSession(FakeSession):
        def request(self, method, url, **kwargs):
            if url == xianyu_login.QUERY_URL:
                self.calls.append((method, url, kwargs))
                self.cookies.values.extend(
                    [cookie("unb", "654321"), cookie("cookie2", "cookie-confirmed")]
                )
                return Response(
                    url,
                    {"content": {"success": True, "data": {"qrCodeStatus": "CONFIRMED"}}},
                )
            if url == xianyu_login.LOGIN_URL:
                raise AssertionError("token continuation must be skipped")
            return super().request(method, url, **kwargs)

    cookie_confirmed_session = CookieConfirmedSession()
    cookie_confirmed = manager(clock, [cookie_confirmed_session], cooldown=0)
    cookie_confirmed_id = cookie_confirmed.start(11)["login_id"]
    assert cookie_confirmed.poll(11, cookie_confirmed_id)["status"] == "confirmed"
    assert "unb=654321" in cookie_confirmed.begin_consume(11, cookie_confirmed_id)
    cookie_confirmed.finish_consume(11, cookie_confirmed_id, True)

    # TTL expiry clears all secret material and returns a bounded public state.
    clock.value += 6
    expiring_session = FakeSession()
    expiring = manager(clock, [expiring_session], cooldown=0)
    expired_id = expiring.start(20)["login_id"]
    clock.value += 151
    deadline = time.time() + 1
    while not expiring_session.closed and time.time() < deadline:
        time.sleep(0.01)
    assert expiring_session.closed is True, "sweeper must actively close expired sessions"
    expired_status = expiring.poll(20, expired_id)
    assert expired_status == {"login_id": expired_id, "status": "expired", "expires_in": 0}
    expect_code("login_expired", lambda: expiring.consume(20, expired_id))
    assert list(expiring_session.cookies) == []

    cancelled = manager(clock, [FakeSession(["CANCELED"])], cooldown=0)
    cancelled_id = cancelled.start(21)["login_id"]
    assert cancelled.poll(21, cancelled_id)["status"] == "expired"

    # A blocked poll cannot be entered twice for the same login.
    gate = threading.Event()
    blocked_session = FakeSession(["NEW"], blocker=gate)
    concurrent = manager(clock, [blocked_session], cooldown=0)
    concurrent_id = concurrent.start(30)["login_id"]
    outcome = []
    thread = threading.Thread(
        target=lambda: outcome.append(concurrent.poll(30, concurrent_id)), daemon=True
    )
    thread.start()
    while len(blocked_session.calls) < 2:
        pass
    expect_code("login_busy", lambda: concurrent.poll(30, concurrent_id))
    gate.set()
    thread.join(timeout=2)
    assert outcome[0]["status"] == "waiting"

    # A global non-blocking gate bounds upstream calls across different users.
    upstream_gate = threading.BoundedSemaphore(1)
    shared_blocker = threading.Event()
    first_shared = FakeSession(["NEW"], blocker=shared_blocker)
    second_shared = FakeSession(["NEW"])
    shared = manager(
        clock,
        [first_shared, second_shared],
        cooldown=0,
        upstream_gate=upstream_gate,
    )
    first_shared_id = shared.start(31)["login_id"]
    second_shared_id = shared.start(32)["login_id"]
    shared_outcome = []
    shared_thread = threading.Thread(
        target=lambda: shared_outcome.append(shared.poll(31, first_shared_id)),
        daemon=True,
    )
    shared_thread.start()
    deadline = time.time() + 1
    while len(first_shared.calls) < 2 and time.time() < deadline:
        time.sleep(0.001)
    expect_code("login_busy", lambda: shared.poll(32, second_shared_id))
    shared_blocker.set()
    shared_thread.join(timeout=2)
    assert shared_outcome[0]["status"] == "waiting"
    assert shared.poll(32, second_shared_id)["status"] == "waiting"

    # Capacity and per-user creation cooldown are independent controls.
    limited = manager(clock, [FakeSession(), FakeSession(), FakeSession()], capacity=1)
    limited_id = limited.start(40)["login_id"]
    expect_code("login_capacity", lambda: limited.start(41))
    limited.cancel(40, limited_id)
    expect_code("login_cooldown", lambda: limited.start(40))
    limited.start(41)

    replacement = manager(clock, [FakeSession(), FakeSession()], cooldown=5)
    replacement.start(42)
    replacement.clear_user(42)
    assert replacement.start(42)["status"] == "waiting"

    # Logout must clear every account scope owned by one user, while the
    # normal clear_user operation remains limited to the selected account.
    all_scopes = manager(clock, [FakeSession(), FakeSession()], cooldown=0)
    default_scope_id = all_scopes.start(43, "default")["login_id"]
    secondary_scope_id = all_scopes.start(43, "secondary")["login_id"]
    all_scopes.clear_user_all(43)
    expect_code("login_not_found", lambda: all_scopes.poll(43, default_scope_id, "default"))
    expect_code("login_not_found", lambda: all_scopes.poll(43, secondary_scope_id, "secondary"))

    # QR URLs, JSON shapes and generated SVG are strict and fail closed.
    class MaliciousSession(FakeSession):
        def request(self, method, url, **kwargs):
            return Response(
                url,
                {
                    "content": {
                        "success": True,
                        "data": {
                            "codeContent": "https://passport.goofish.com.evil.test/newlogin/x",
                            "t": 1700000000000,
                            "ck": "query-check-contract-1234567890",
                        },
                    }
                },
            )

    malicious = manager(clock, [MaliciousSession()], cooldown=0)
    expect_code("platform_error", lambda: malicious.start(50))

    class StreamingBombResponse(Response):
        def __init__(self, url):
            super().__init__(url, {})
            self.headers = {}

        def iter_content(self, chunk_size, decode_unicode=False):
            assert chunk_size == 16 * 1024 and decode_unicode is False
            yield b"{" + b"x" * xianyu_login.MAX_RESPONSE_BYTES

    class StreamingBombSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.response = None

        def request(self, method, url, **kwargs):
            assert kwargs["stream"] is True
            self.response = StreamingBombResponse(url)
            return self.response

    streaming_bomb_session = StreamingBombSession()
    streaming_bomb = manager(clock, [streaming_bomb_session], cooldown=0)
    expect_code("platform_error", lambda: streaming_bomb.start(52))
    assert streaming_bomb_session.response.closed is True

    bad_svg = xianyu_login.XianyuLoginManager(
        session_factory=lambda: FakeSession(),
        qr_factory=lambda _url: b'<svg><script>alert(1)</script></svg>',
        clock=clock,
        cooldown_seconds=0,
    )
    expect_code("platform_error", lambda: bad_svg.start(51))

    # Malicious confirmation fields never reach the login endpoint or public state.
    bad_confirm_session = FakeSession(["CONFIRMED"])
    original_request = bad_confirm_session.request

    def malformed_confirm(method, url, **kwargs):
        if url == xianyu_login.QUERY_URL:
            return Response(
                url,
                {
                    "content": {
                        "success": True,
                        "data": {"qrCodeStatus": "CONFIRMED", "loginToken": "bad\nvalue"},
                    }
                },
            )
        return original_request(method, url, **kwargs)

    bad_confirm_session.request = malformed_confirm
    bad_confirm = manager(clock, [bad_confirm_session], cooldown=0)
    bad_id = bad_confirm.start(60)["login_id"]
    public = bad_confirm.poll(60, bad_id)
    assert public["status"] == "error" and public["code"] == "login_confirm_failed"
    assert "loginToken" not in repr(public) and "bad" not in repr(public)
    expect_code("login_not_found", lambda: bad_confirm.poll(60, bad_id))

    class QueryFailureSession(FakeSession):
        def request(self, method, url, **kwargs):
            if url == xianyu_login.QUERY_URL:
                return Response(url, {"content": {"success": False}})
            return super().request(method, url, **kwargs)

    query_failure = manager(clock, [QueryFailureSession()], cooldown=0)
    query_failure_id = query_failure.start(61)["login_id"]
    query_public = query_failure.poll(61, query_failure_id)
    assert query_public["status"] == "error"
    assert query_public["code"] == "qr_query_failed"

    class LoginConfirmFailureSession(FakeSession):
        def request(self, method, url, **kwargs):
            if url == xianyu_login.LOGIN_URL:
                return Response(url, {"content": {"success": False}})
            return super().request(method, url, **kwargs)

    confirm_failure = manager(
        clock, [LoginConfirmFailureSession(["CONFIRMED"])], cooldown=0
    )
    confirm_failure_id = confirm_failure.start(62)["login_id"]
    confirm_public = confirm_failure.poll(62, confirm_failure_id)
    assert confirm_public["status"] == "error"
    assert confirm_public["code"] == "login_confirm_failed"

    class MtopFailureSession(FakeSession):
        def request(self, method, url, **kwargs):
            if url == xianyu_login.MTOP_URL:
                return Response(url, {}, status=502)
            return super().request(method, url, **kwargs)

    mtop_failure = manager(clock, [MtopFailureSession(["CONFIRMED"])], cooldown=0)
    mtop_failure_id = mtop_failure.start(63)["login_id"]
    mtop_public = mtop_failure.poll(63, mtop_failure_id)
    assert mtop_public["status"] == "error"
    assert mtop_public["code"] == "mtop_context_failed"

    class CookieIncompleteSession(FakeSession):
        def request(self, method, url, **kwargs):
            if url == xianyu_login.MTOP_URL:
                return Response(url, {"ret": ["FAIL_SYS_TOKEN_EXPIRED"], "data": {}})
            return super().request(method, url, **kwargs)

    cookie_incomplete = manager(
        clock, [CookieIncompleteSession(["CONFIRMED"])], cooldown=0
    )
    cookie_incomplete_id = cookie_incomplete.start(64)["login_id"]
    cookie_public = cookie_incomplete.poll(64, cookie_incomplete_id)
    assert cookie_public["status"] == "error"
    assert cookie_public["code"] == "qr_cookie_incomplete"
    assert "cookie2" not in repr(cookie_public).lower()
    assert "_m_h5_tk" not in repr(cookie_public).lower()

    print(
        "xianyu-login-contract: isolation, active TTL, bounded streaming, concurrency and two-phase Cookie passed"
    )


if __name__ == "__main__":
    main()
