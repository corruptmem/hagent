"""Ingress rules for the management UI (design section 6.1).

- ``IngressGuard`` accepts connections only from the Supervisor address.
- ``UiComponent`` serves the built UI on relative URLs under the
  ``X-Ingress-Path`` base path.
"""

from hagent_app.ingress.config import IngressConfig
from hagent_app.ingress.guard import IngressGuard
from hagent_app.ingress.ui import UiComponent

__all__ = ["IngressConfig", "IngressGuard", "UiComponent"]
