"""Verify that the application package loads independently of Odoo."""

import sys
from importlib import import_module


def test_application_import_does_not_load_odoo() -> None:
    """Import the application package without loading the Odoo runtime."""
    import_module("application")

    assert "odoo" not in sys.modules
