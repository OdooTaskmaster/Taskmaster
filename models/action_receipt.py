"""Declare the internal table for transactional confirmed-write receipts."""

from odoo import fields, models


class ActionReceipt(models.Model):
    """Store opaque keys and references; no client access is granted."""

    _name = "taskmaster.action.receipt"
    _description = "Taskmaster action receipt"
    _log_access = False

    action_key = fields.Char(required=True)
    reference = fields.Char()
    _unique_action_key = models.Constraint("UNIQUE(action_key)", "An action receipt already exists.")
