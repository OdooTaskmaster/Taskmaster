"""Import core contracts from the add-on package or the unit-test root."""

if __package__ and "." in __package__:  # Odoo imports odoo.addons.taskmaster.adapters
    from ..application.errors import ErrorCategory  # noqa: TID252
    from ..application.model_port import (  # noqa: TID252
        ModelFailure,
        ModelRequest,
        ModelResponse,
    )
else:  # pytest imports adapters from the add-on root
    from application.errors import ErrorCategory
    from application.model_port import ModelFailure, ModelRequest, ModelResponse

__all__ = ["ErrorCategory", "ModelFailure", "ModelRequest", "ModelResponse"]
