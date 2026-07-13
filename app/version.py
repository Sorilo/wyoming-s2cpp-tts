"""Canonical application version — single source of truth.

All runtime version reporting, Docker image labels, CI workflows, and
manifest generators MUST reference this value.  Do not duplicate version
strings elsewhere.
"""

__version__ = "0.1.0"
