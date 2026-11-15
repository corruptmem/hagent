"""The Ingress source-address check (design section 6.1).

Ingress forwards each request from the Supervisor address, ``172.30.32.2``.
The guard refuses every connection from another address, for the API, the
UI files and the event WebSocket.

The guard uses the address of the TCP peer. It never reads
``X-Forwarded-For`` or a similar header, because a client can set them.

The guard refuses every request until it starts and has read its options.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from aiohttp import web
from aiohttp.typedefs import Handler

from hagent_app.config import ConfigError
from hagent_app.http.errors import ApiError
from hagent_app.ingress.config import IngressConfig

if TYPE_CHECKING:
    from hagent_app.app import AppContext

_LOG = logging.getLogger(__name__)

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(values: Iterable[str]) -> tuple[Network, ...]:
    """Parse addresses and CIDR networks. Raise ``ConfigError`` if one is bad."""
    if isinstance(values, str):
        msg = "option 'ingress.allowed_addresses' must be a list"
        raise ConfigError(msg)
    networks = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(str(value), strict=False))
        except ValueError as err:
            msg = f"option 'ingress.allowed_addresses' is not valid: {value!r}"
            raise ConfigError(msg) from err
    return tuple(networks)


def address_allowed(remote: str | None, networks: Sequence[Network]) -> bool:
    """True if ``remote`` is in one of ``networks``."""
    if not remote:
        return False
    try:
        address = ipaddress.ip_address(remote.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


class IngressGuard:
    """A component with the source-address middleware.

    Add the middleware in ``default_components()``, before the server
    starts: ``http.add_middleware(guard.middleware)``.

    ``exempt_prefixes`` lists path prefixes that the guard does not check.
    ``exempt_paths`` lists exact paths that the guard does not check. A
    connection that does not come through Ingress must authenticate itself.
    The integration link uses ``exempt_paths``, so that no subpath of the
    link path escapes the check.
    """

    name = "ingress_guard"

    def __init__(
        self,
        *,
        exempt_prefixes: Sequence[str] = (),
        exempt_paths: Sequence[str] = (),
    ) -> None:
        self._exempt = tuple(exempt_prefixes)
        self._exempt_exact = frozenset(exempt_paths)
        self._networks: tuple[Network, ...] | None = None

    def _exempt_path(self, path: str) -> bool:
        if path in self._exempt_exact:
            return True
        # A prefix matches whole path segments: "/link" exempts "/link" and
        # "/link/ws", not "/linkx".
        return any(
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
            for prefix in self._exempt
        )

    async def start(self, ctx: AppContext) -> None:
        """Read the allowed addresses from the ``ingress`` options."""
        config = ctx.config.section("ingress", IngressConfig)
        self._networks = parse_networks(config.allowed_addresses)
        if [str(n) for n in self._networks] != ["172.30.32.2/32"]:
            _LOG.warning(
                "Ingress guard allows other addresses than the Supervisor: %s",
                ", ".join(str(n) for n in self._networks),
            )

    async def stop(self) -> None:
        """Refuse every request again."""
        self._networks = None

    @web.middleware
    async def middleware(
        self, request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        """Refuse the request if the peer address is not allowed."""
        if self._exempt_path(request.path):
            return await handler(request)
        networks = self._networks
        if networks is None:
            raise ApiError(503, "not_ready", "the app is starting or stopping")
        if not address_allowed(request.remote, networks):
            _LOG.warning("refused a connection from %s", request.remote)
            raise ApiError(
                403, "forbidden", "the app accepts connections only through Ingress"
            )
        return await handler(request)
