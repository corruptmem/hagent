import asyncio
from pathlib import Path

from hagent_app.app import App, AppContext
from hagent_app.config import Config
from hagent_app.ha import HAClient
from helpers.fake_ha import FakeHA
from helpers.wait import wait_until


class _Recorder:
    name = "recorder"

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.ctx: AppContext | None = None

    async def start(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.log.append("start")

    async def stop(self) -> None:
        self.log.append("stop")


def _config(fake_ha: FakeHA, data_dir: Path) -> Config:
    return Config(ha_url=fake_ha.url, ha_token=fake_ha.token, data_dir=data_dir)


async def test_app_starts_survives_ha_restart_and_stops(
    fake_ha: FakeHA, tmp_path: Path
) -> None:
    fake_ha.set_state("light.one", "off")
    config = _config(fake_ha, tmp_path / "data")
    client = HAClient(config.ha_url, config.ha_token, backoff_initial=0.01)
    log: list[str] = []
    recorder = _Recorder(log)
    app = App(config, [recorder], client=client)
    ctx = await app.start()
    try:
        assert recorder.ctx is ctx
        assert config.database_path.exists()
        await asyncio.wait_for(ctx.states.wait_ready(), 2)
        assert ctx.states.get("light.one") is not None

        await fake_ha.restart()
        await wait_until(lambda: not ctx.ha.connected)
        fake_ha.up()
        await asyncio.wait_for(ctx.ha.wait_connected(), 2)
        await fake_ha.set_state("light.one", "on")
        await wait_until(
            lambda: (s := ctx.states.get("light.one")) is not None and s.state == "on"
        )
        assert fake_ha.connections_accepted == 2
    finally:
        await app.stop()
    assert log == ["start", "stop"]
    assert fake_ha.connection_count == 0


async def test_installation_id_is_stable(fake_ha: FakeHA, tmp_path: Path) -> None:
    config = _config(fake_ha, tmp_path)
    ids = []
    for _ in range(2):
        app = App(config, [])
        ctx = await app.start()
        ids.append(ctx.installation_id)
        await app.stop()
    assert ids[0] == ids[1]
    assert len(ids[0]) == 32


async def test_run_stops_on_event(fake_ha: FakeHA, tmp_path: Path) -> None:
    stop = asyncio.Event()
    app = App(_config(fake_ha, tmp_path), [])
    task = asyncio.create_task(app.run(stop))
    await wait_until(lambda: app.ctx is not None and app.ctx.ha.connected)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert app.ctx is None
