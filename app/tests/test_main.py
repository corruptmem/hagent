"""Run ``python -m hagent_app`` as a process against the fake HA server."""

import asyncio
import os
import signal
import sys
from pathlib import Path

from helpers.fake_ha import FakeHA
from helpers.wait import wait_until


async def test_process_connects_reconnects_and_stops_on_sigterm(
    fake_ha: FakeHA, tmp_path: Path
) -> None:
    env = {
        **os.environ,
        "HAGENT_DATA_DIR": str(tmp_path),
        "HAGENT_HA_URL": fake_ha.url,
        "SUPERVISOR_TOKEN": fake_ha.token,
    }
    env.pop("HAGENT_HA_TOKEN", None)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "hagent_app",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await wait_until(lambda: fake_ha.connections_accepted == 1, limit=10)
        await wait_until(lambda: bool(fake_ha.commands_of("get_states")), limit=5)
        await fake_ha.restart()
        fake_ha.up()
        await wait_until(lambda: fake_ha.connections_accepted == 2, limit=10)
        await wait_until(
            lambda: len(fake_ha.commands_of("get_states")) == 2,
            limit=5,
        )
        proc.send_signal(signal.SIGTERM)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode == 0, out.decode()
    assert (tmp_path / "hagent.db").exists()
    assert fake_ha.connection_count == 0


async def test_process_exits_with_config_error(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_TOKEN")}
    env["HAGENT_DATA_DIR"] = str(tmp_path)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "hagent_app",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), 10)
    assert proc.returncode == 2
    assert b"configuration error" in out
