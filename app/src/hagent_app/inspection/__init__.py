"""The inspect area of the management API (design section 7.2).

``InspectComponent`` serves read-only routes for invocations and their
traces, the history search, the audit log, permissions, schedules and
cost. It publishes the ``invocations`` change events.
"""

from hagent_app.inspection.component import InspectComponent

__all__ = ["InspectComponent"]
