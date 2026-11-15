"""The ``ingress`` section of the app options."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SUPERVISOR_ADDRESS = "172.30.32.2"

# In a source checkout, the UI build is in ``frontend/dist`` at the root of
# the repository. The app image keeps the same layout in /opt/hagent, so
# this path also holds there (see hagent/Dockerfile).
SOURCE_UI_DIR = Path(__file__).resolve().parents[4] / "frontend" / "dist"


@dataclass(frozen=True)
class IngressConfig:
    """Options for the Ingress source check and the UI files.

    ``allowed_addresses`` lists the addresses and networks (CIDR) that can
    connect. The default is the Supervisor address only. Change it only for
    local development, for example to ``["127.0.0.1"]``.

    ``ui_dir`` is the directory of the built UI. An empty value selects
    ``frontend/dist`` in the source checkout.
    """

    allowed_addresses: list[str] = field(default_factory=lambda: [SUPERVISOR_ADDRESS])
    ui_dir: str = ""

    def ui_path(self) -> Path:
        """The UI directory."""
        return Path(self.ui_dir) if self.ui_dir else SOURCE_UI_DIR
