from __future__ import annotations

import socket
import threading
import webbrowser

import pytest
from conftest import RECORDED_BROWSER_CALLS


def test_webbrowser_open_records_and_launches_nothing() -> None:
    initial_count = len(RECORDED_BROWSER_CALLS)
    target_url = "https://accounts.google.com/o/oauth2/v2/auth?client_id=synthetic"

    result_open = webbrowser.open(target_url)
    assert result_open is True
    assert len(RECORDED_BROWSER_CALLS) == initial_count + 1
    assert RECORDED_BROWSER_CALLS[-1].url == target_url
    assert RECORDED_BROWSER_CALLS[-1].caller == "webbrowser.open"

    result_new = webbrowser.open_new("https://slack.com/oauth/v2_user/authorize")
    assert result_new is True
    assert len(RECORDED_BROWSER_CALLS) == initial_count + 2
    assert RECORDED_BROWSER_CALLS[-1].url == "https://slack.com/oauth/v2_user/authorize"
    assert RECORDED_BROWSER_CALLS[-1].caller == "webbrowser.open_new"

    result_tab = webbrowser.open_new_tab(
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    )
    assert result_tab is True
    assert len(RECORDED_BROWSER_CALLS) == initial_count + 3
    assert (
        RECORDED_BROWSER_CALLS[-1].url
        == "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    )
    assert RECORDED_BROWSER_CALLS[-1].caller == "webbrowser.open_new_tab"

    controller = webbrowser.get("firefox")
    result_ctrl = controller.open("https://example.com/auth")
    assert result_ctrl is True
    assert len(RECORDED_BROWSER_CALLS) == initial_count + 4
    assert RECORDED_BROWSER_CALLS[-1].url == "https://example.com/auth"
    assert RECORDED_BROWSER_CALLS[-1].caller == "controller.open"
    assert RECORDED_BROWSER_CALLS[-1].browser_name == "firefox"


def test_socket_connect_to_external_address_raises_guard_error() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match=r"test tried to reach non-loopback address"):
            sock.connect(("93.184.216.34", 443))

        with pytest.raises(RuntimeError, match=r"test tried to reach non-loopback address"):
            sock.connect_ex(("93.184.216.34", 443))
    finally:
        sock.close()


def test_socket_connect_to_loopback_listener_succeeds() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    received: list[bytes] = []

    def serve() -> None:
        conn, _ = server.accept()
        try:
            data = conn.recv(1024)
            received.append(data)
            conn.sendall(b"pong")
        finally:
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(("127.0.0.1", port))
        client.sendall(b"ping")
        response = client.recv(1024)
        assert response == b"pong"
    finally:
        client.close()
        server.close()

    thread.join(timeout=2.0)
    assert received == [b"ping"]
