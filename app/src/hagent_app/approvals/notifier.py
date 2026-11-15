"""Send approval requests to the phones of the approvers.

Terms:

- **Registration**: one mobile_app config entry. The HA Companion app
  creates it when a person signs in on a phone. HA adds one device for it to
  the device registry.
- **Notify target**: a legacy notify service ``notify.mobile_app_<slug>``.
  HA derives ``<slug>`` from the device name of the registration.

``Notifier`` is the interface. ``MobileAppNotifier`` is the app-side
implementation. The hagent integration (task-17) can add a sender in HA
Core later. That sender can address registrations by webhook ID.

The app cannot read the registration data (the HA user ID and the webhook
ID) over the WebSocket API. ``MobileAppNotifier`` therefore resolves an
approver in one of two ways:

1. An explicit ``notify_services`` list in the settings, if the approver
   has one.
2. The person chain: each ``person`` entity whose ``user_id`` attribute is
   the approver, then each of its ``device_trackers`` that the mobile_app
   integration owns, then the device of that tracker.

For each target, the notifier checks two things. If one fails, it does not
send to that target, and it records the reason:

- The service exists in HA.
- Exactly one mobile_app device has the slug of the service. The legacy
  service keys its targets by device name, so two devices with the same
  name route to one of them, and HA reports no error (task-4).

Limit: the user can link any device tracker to a person. A request can
therefore reach a phone of another person. That phone cannot approve: the
answer check uses the HA user of the registration that fires the answer
event, not the delivery target.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from slugify import slugify

from hagent_app.ha import HAClient, StateCache
from hagent_app.ha.client import HAError

_LOG = logging.getLogger(__name__)

MOBILE_APP = "mobile_app"
SERVICE_PREFIX = "mobile_app_"


@dataclass(frozen=True, slots=True)
class ApprovalMessage:
    """One notification for one approval request.

    ``tag`` lets the app replace or clear the notification later.
    ``accept_action`` and ``reject_action`` are the action IDs of the two
    buttons.
    """

    request_id: str
    tag: str
    title: str
    message: str
    accept_action: str
    reject_action: str


@dataclass(frozen=True, slots=True)
class Delivery:
    """The result of a send: the targets that HA accepted, and the problems."""

    targets: tuple[str, ...]
    problems: tuple[str, ...]


class Notifier(Protocol):
    """Send and clear approval notifications."""

    async def send(
        self, message: ApprovalMessage, approvers: Sequence[str]
    ) -> Delivery:
        """Send the message to the registrations of the approvers."""
        ...

    async def clear(self, message: ApprovalMessage, targets: Sequence[str]) -> None:
        """Remove the notification from each target. Never raise."""
        ...


def ha_slug(name: str) -> str:
    """The slug that HA Core makes from a device name (``util.slugify``)."""
    slug = slugify(name, separator="_")
    return slug or "unknown"


class MobileAppNotifier:
    """The app-side notifier. See the module text for the resolution rules."""

    def __init__(
        self,
        ha: HAClient,
        states: StateCache,
        overrides: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._ha = ha
        self._states = states
        self._overrides = {k: tuple(v) for k, v in (overrides or {}).items()}

    async def resolve(self, approvers: Sequence[str]) -> Delivery:
        """The notify targets of the approvers, with the reasons for gaps."""
        entities = await self._ha.command({"type": "config/entity_registry/list"})
        devices = await self._ha.command({"type": "config/device_registry/list"})
        services = await self._ha.command({"type": "get_services"})
        notify = (services or {}).get("notify") or {}
        mobile = {
            d["id"]: d
            for d in devices or []
            if isinstance(d, Mapping) and _is_mobile_app(d) and d.get("id")
        }
        slug_count = Counter(
            ha_slug(str(d["name"])) for d in mobile.values() if d.get("name")
        )
        trackers = {
            e["entity_id"]: e
            for e in entities or []
            if isinstance(e, Mapping)
            and e.get("platform") == MOBILE_APP
            and isinstance(e.get("entity_id"), str)
        }

        targets: list[str] = []
        problems: list[str] = []

        def check(user_id: str, service: str) -> bool:
            slug = service.removeprefix(SERVICE_PREFIX)
            count = slug_count.get(slug, 0)
            if count != 1:
                problems.append(
                    f"approver {user_id}: notify.{service} matches {count} "
                    "mobile_app devices, not exactly 1"
                )
                return False
            if service not in notify:
                problems.append(f"approver {user_id}: notify.{service} does not exist")
                return False
            return True

        for user_id in approvers:
            found: list[str] = []
            if user_id in self._overrides:
                found = [s for s in self._overrides[user_id] if check(user_id, s)]
            else:
                for device_id in self._person_devices(user_id, trackers, problems):
                    device = mobile.get(device_id)
                    if device is None or not device.get("name"):
                        problems.append(
                            f"approver {user_id}: tracker device is not a "
                            "mobile_app device with a name"
                        )
                        continue
                    service = SERVICE_PREFIX + ha_slug(str(device["name"]))
                    if check(user_id, service):
                        found.append(service)
            if not found:
                problems.append(f"approver {user_id}: no notify target")
            targets.extend(s for s in found if s not in targets)
        return Delivery(tuple(targets), tuple(problems))

    def _person_devices(
        self,
        user_id: str,
        trackers: Mapping[str, Mapping[str, Any]],
        problems: list[str],
    ) -> list[str]:
        device_ids: list[str] = []
        persons = [
            s
            for s in self._states
            if s.entity_id.startswith("person.")
            and s.attributes.get("user_id") == user_id
        ]
        if not persons:
            problems.append(f"approver {user_id}: no person entity links this user")
        for person in persons:
            linked = person.attributes.get("device_trackers") or []
            for tracker_id in linked if isinstance(linked, list) else []:
                entry = trackers.get(tracker_id)
                if entry is None:
                    continue  # Not a mobile_app tracker.
                device_id = entry.get("device_id")
                if isinstance(device_id, str) and device_id not in device_ids:
                    device_ids.append(device_id)
        return device_ids

    async def send(
        self, message: ApprovalMessage, approvers: Sequence[str]
    ) -> Delivery:
        """Resolve the targets and send. Return the targets that HA accepted."""
        try:
            resolved = await self.resolve(approvers)
        except (HAError, TimeoutError) as err:
            return Delivery((), (f"cannot read the HA registries: {err}",))
        sent: list[str] = []
        problems = list(resolved.problems)
        for service in resolved.targets:
            try:
                await self._ha.call_service("notify", service, _payload(message))
            except (HAError, TimeoutError) as err:
                problems.append(f"notify.{service} failed: {err}")
                continue
            sent.append(service)
        return Delivery(tuple(sent), tuple(problems))

    async def clear(self, message: ApprovalMessage, targets: Sequence[str]) -> None:
        """Send ``clear_notification`` with the tag to each target."""
        for service in targets:
            try:
                await self._ha.call_service(
                    "notify",
                    service,
                    {"message": "clear_notification", "data": {"tag": message.tag}},
                )
            except (HAError, TimeoutError) as err:
                _LOG.warning(
                    "cannot clear approval notification on %s: %s", service, err
                )


def _is_mobile_app(device: Mapping[str, Any]) -> bool:
    for ident in device.get("identifiers") or []:
        if isinstance(ident, list | tuple) and ident and ident[0] == MOBILE_APP:
            return True
    return False


def _payload(message: ApprovalMessage) -> dict[str, Any]:
    return {
        "title": message.title,
        "message": message.message,
        "data": {
            "tag": message.tag,
            "actions": [
                {
                    "action": message.accept_action,
                    "title": "Accept",
                    "authenticationRequired": True,
                },
                {
                    "action": message.reject_action,
                    "title": "Reject",
                    "destructive": True,
                },
            ],
        },
    }
