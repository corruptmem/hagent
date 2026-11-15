"""Ingress rules: the source-address guard and the UI files (section 6.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from hagent_app.app import App, default_components
from hagent_app.config import Config, ConfigError
from hagent_app.http import HttpServer
from hagent_app.ingress import IngressGuard
from hagent_app.ingress.guard import address_allowed, parse_networks
from hagent_app.ingress.ui import ingress_base, inject_base
from helpers.fake_ha import TOKEN, FakeHA
from yarl import URL

INDEX = (
    '<!doctype html><html lang="en"><head><meta charset="UTF-8">'
    '<script type="module" src="./assets/index-abc.js"></script></head>'
    '<body><div id="root"></div></body></html>'
)
INGRESS = "/api/hassio_ingress/abcDEF123-_x"


def _config(fake_ha: FakeHA, data_dir: Path, **ingress: Any) -> Config:
    return Config(
        ha_url=fake_ha.url,
        ha_token=fake_ha.token,
        data_dir=data_dir,
        port=0,
        options={"ingress": ingress} if ingress else {},
    )


def _ui_dir(tmp_path: Path) -> Path:
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text(INDEX, encoding="utf-8")
    (ui / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    (ui / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    return ui


def _request(remote: str) -> web.Request:
    transport = MagicMock()
    transport.get_extra_info.side_effect = lambda name, default=None: (
        (remote, 40000) if name == "peername" else default
    )
    return make_mocked_request("GET", "/api/agents", transport=transport)


async def _ok(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


class _Ctx:
    def __init__(self, options: dict[str, Any]) -> None:
        self.config = Config(
            ha_url="ws://x", ha_token=TOKEN, data_dir=Path(), options=options
        )


# ------------------------------------------------------------- the guard


@pytest.mark.parametrize(
    ("remote", "allowed"),
    [
        ("172.30.32.2", True),
        ("::ffff:172.30.32.2", True),
        ("172.30.32.1", False),
        ("172.30.33.2", False),
        ("127.0.0.1", False),
        ("::1", False),
        ("", False),
        ("not-an-ip", False),
    ],
)
async def test_guard_allows_only_the_supervisor_by_default(
    remote: str, allowed: bool
) -> None:
    guard = IngressGuard()
    await guard.start(_Ctx({}))  # type: ignore[arg-type]
    request = _request(remote)
    if allowed:
        response = await guard.middleware(request, _ok)
        assert response.status == 200
    else:
        with pytest.raises(Exception) as info:
            await guard.middleware(request, _ok)
        assert getattr(info.value, "status", None) == 403


async def test_guard_refuses_everything_before_start_and_after_stop() -> None:
    guard = IngressGuard()
    with pytest.raises(Exception) as info:
        await guard.middleware(_request("172.30.32.2"), _ok)
    assert getattr(info.value, "status", None) == 503
    await guard.start(_Ctx({}))  # type: ignore[arg-type]
    await guard.stop()
    with pytest.raises(Exception) as info:
        await guard.middleware(_request("172.30.32.2"), _ok)
    assert getattr(info.value, "status", None) == 503


async def test_guard_override_accepts_networks() -> None:
    guard = IngressGuard()
    ctx = _Ctx({"ingress": {"allowed_addresses": ["127.0.0.0/8", "::1"]}})
    await guard.start(ctx)  # type: ignore[arg-type]
    for remote in ("127.0.0.1", "127.1.2.3", "::1"):
        assert (await guard.middleware(_request(remote), _ok)).status == 200
    with pytest.raises(Exception) as info:
        await guard.middleware(_request("172.30.32.2"), _ok)
    assert getattr(info.value, "status", None) == 403


def test_bad_allowed_addresses_stop_the_start() -> None:
    with pytest.raises(ConfigError):
        parse_networks(["172.30.32.300"])
    with pytest.raises(ConfigError):
        parse_networks("172.30.32.2")
    assert not address_allowed("fe80::1%eth0", parse_networks(["172.30.32.2"]))


async def test_guard_exempt_prefix() -> None:
    guard = IngressGuard(exempt_prefixes=["/link"])
    await guard.start(_Ctx({}))  # type: ignore[arg-type]
    transport = MagicMock()
    transport.get_extra_info.side_effect = lambda name, default=None: (
        ("10.0.0.5", 1) if name == "peername" else default
    )
    request = make_mocked_request("GET", "/link/ws", transport=transport)
    assert (await guard.middleware(request, _ok)).status == 200
    request = make_mocked_request("GET", "/linkx", transport=transport)
    with pytest.raises(Exception) as info:
        await guard.middleware(request, _ok)
    assert getattr(info.value, "status", None) == 403
    # Review L1: the guard matches the raw path; an encoded separator is
    # never exempt, also below an exempt prefix.
    for path in ("/link%2Fws", "/link/a%2f..%2fb", "/link%5Cws"):
        request = make_mocked_request("GET", path, transport=transport)
        with pytest.raises(Exception) as info:
            await guard.middleware(request, _ok)
        assert getattr(info.value, "status", None) == 403, path


async def test_default_app_refuses_local_clients(
    fake_ha: FakeHA, tmp_path: Path
) -> None:
    """With the default options, a client on 127.0.0.1 gets nothing."""
    components = default_components()
    app = App(_config(fake_ha, tmp_path), components)
    await app.start()
    try:
        http = components[-1]
        assert isinstance(http, HttpServer)
        base = f"http://127.0.0.1:{http.port}"
        async with aiohttp.ClientSession() as session:
            for path in ("/api/agents", "/api/ha/catalog", "/", "/agents/x"):
                async with session.get(base + path) as resp:
                    assert resp.status == 403, path
                    body = await resp.json()
                    assert body["error"]["code"] == "forbidden"
            async with session.post(
                base + "/api/agents", json={"definition": {}}
            ) as resp:
                assert resp.status == 403
            with pytest.raises(aiohttp.WSServerHandshakeError) as info:
                await session.ws_connect(base + "/api/events")
            assert info.value.status == 403
    finally:
        await app.stop()


# ----------------------------------------------------------- the UI files


def test_ingress_base_accepts_only_plain_paths() -> None:
    assert ingress_base(None) == "/"
    assert ingress_base("") == "/"
    assert ingress_base(INGRESS) == INGRESS + "/"
    assert ingress_base(INGRESS + "/") == INGRESS + "/"
    for bad in ('/x"><script>', "/x y", "x/y", "//evil.example", "/a//b"):
        assert ingress_base(bad) == "/"


def test_inject_base_puts_base_first_in_head() -> None:
    out = inject_base(INDEX, INGRESS + "/")
    assert f'<head><base href="{INGRESS}/"><meta' in out
    assert inject_base("<p>x</p>", "/").startswith('<base href="/">')


@pytest.fixture
async def ui_server(fake_ha: FakeHA, tmp_path: Path) -> Any:
    components = default_components()
    config = _config(
        fake_ha,
        tmp_path / "data",
        allowed_addresses=["127.0.0.1"],
        ui_dir=str(_ui_dir(tmp_path)),
    )
    app = App(config, components)
    await app.start()
    http = components[-1]
    assert isinstance(http, HttpServer)
    try:
        yield f"http://127.0.0.1:{http.port}"
    finally:
        await app.stop()


async def test_ui_loads_with_ingress_base(ui_server: str) -> None:
    headers = {"X-Ingress-Path": INGRESS}
    async with aiohttp.ClientSession() as session:
        for path in ("/", "/agents", "/agents/0123/versions"):
            async with session.get(ui_server + path, headers=headers) as resp:
                assert resp.status == 200, path
                assert resp.content_type == "text/html"
                text = await resp.text()
                assert f'<base href="{INGRESS}/">' in text
                # Vite builds with base "./": every asset URL is relative.
                assert 'src="./assets/index-abc.js"' in text
                assert 'src="/' not in text
        async with session.get(ui_server + "/index.html", headers=headers) as resp:
            assert f'<base href="{INGRESS}/">' in await resp.text()
        async with session.get(ui_server + "/assets/index-abc.js") as resp:
            assert resp.status == 200
            assert await resp.text() == "console.log(1)"
            assert "immutable" in resp.headers["Cache-Control"]
        async with session.get(ui_server + "/favicon.svg") as resp:
            assert resp.status == 200
        async with session.get(ui_server + "/") as resp:
            assert '<base href="/">' in await resp.text()


async def test_ui_refuses_missing_files_and_traversal(ui_server: str) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.get(ui_server + "/assets/missing.js") as resp:
            assert resp.status == 404
            assert (await resp.json())["error"]["code"] == "not_found"
        for path in (
            "/../secret.txt",
            "/assets/../../secret.txt",
            "/%2e%2e/secret.txt",
            "/assets/%2e%2e/%2e%2e/secret.txt",
            "/..%5csecret.txt",
        ):
            async with session.get(URL(ui_server + path, encoded=True)) as resp:
                assert await resp.text() != "secret", path
                assert resp.status in {200, 404}, path


async def test_api_paths_never_fall_back_to_the_ui(ui_server: str) -> None:
    async with aiohttp.ClientSession() as session:
        for path in ("/api", "/api/", "/api/missing", "/api/agents/x/y"):
            async with session.get(ui_server + path) as resp:
                assert resp.status == 404, path
                assert (await resp.json())["error"]["code"] == "not_found"


async def test_ui_without_build_shows_a_message(
    fake_ha: FakeHA, tmp_path: Path
) -> None:
    components = default_components()
    config = _config(
        fake_ha,
        tmp_path,
        allowed_addresses=["127.0.0.1"],
        ui_dir=str(tmp_path / "missing"),
    )
    app = App(config, components)
    await app.start()
    try:
        http = components[-1]
        assert isinstance(http, HttpServer)
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{http.port}/") as resp,
        ):
            assert resp.status == 200
            assert "not built" in await resp.text()
    finally:
        await app.stop()
