"""Approval requests for tool calls (design section 8).

``ApprovalsComponent.gate`` is the ``ApprovalGate`` of the tool executor.
See ``service.py`` for the rules and ``notifier.py`` for the delivery.
"""

from hagent_app.approvals.component import ApprovalsComponent
from hagent_app.approvals.config import ApprovalsConfig
from hagent_app.approvals.notifier import (
    ApprovalMessage,
    Delivery,
    MobileAppNotifier,
    Notifier,
)
from hagent_app.approvals.service import ApprovalRecord, ApprovalService

__all__ = [
    "ApprovalMessage",
    "ApprovalRecord",
    "ApprovalService",
    "ApprovalsComponent",
    "ApprovalsConfig",
    "Delivery",
    "MobileAppNotifier",
    "Notifier",
]
