"""Mark the Project AI Conductor add-on package."""

if __name__.startswith("odoo.addons."):
    from . import controllers, models
