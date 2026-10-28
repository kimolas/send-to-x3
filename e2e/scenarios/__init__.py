"""End-to-end scenarios against the real CrossPoint firmware (see e2e/README.md).

Importing this package registers every scenario in :data:`REGISTRY`.
"""

from . import s1_catalog, s2_sleep, s3_lan_push, s4_opds, s5_reader, s6_open, w1_wire
from .base import REGISTRY, Context, Result, Scenario, SkipScenario, execute

__all__ = ["REGISTRY", "Context", "Result", "Scenario", "SkipScenario", "execute",
           "s1_catalog", "s2_sleep", "s3_lan_push", "s4_opds", "s5_reader", "s6_open", "w1_wire"]
