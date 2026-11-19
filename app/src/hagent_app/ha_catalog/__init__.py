"""A read-only catalogue of HA entities, devices, areas, labels and services.

The management UI uses it for its pickers. See ``docs/api.md``.
"""

from hagent_app.ha_catalog.component import (
    REGISTRY_COMMANDS,
    CatalogComponent,
)

__all__ = ["REGISTRY_COMMANDS", "CatalogComponent"]
