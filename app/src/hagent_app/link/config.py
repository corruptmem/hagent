"""Options of the integration link (the ``link`` section of ``options.json``)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LinkConfig:
    """Options of the integration link.

    - ``discovery``: send the link address and the secret to the Supervisor,
      so that HA offers the integration set-up. It applies only inside an
      app container (when ``SUPERVISOR_TOKEN`` is set).
    - ``supervisor_url``: the Supervisor API.
    - ``host``: the host name that the integration connects to. An empty
      value selects the host name of the container.
    - ``refresh_seconds``: the interval of the full status refresh (cost
      today, budget state).
    """

    discovery: bool = True
    supervisor_url: str = "http://supervisor"
    host: str = ""
    refresh_seconds: float = 300.0
