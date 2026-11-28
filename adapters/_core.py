"""Import core contracts from the add-on package or the unit-test root."""

if __package__ and "." in __package__:  # Odoo imports odoo.addons.taskmaster.adapters
    from ..application.errors import ErrorCategory  # noqa: TID252
    from ..application.model_port import (  # noqa: TID252
        ModelFailure,
        ModelRequest,
        ModelResponse,
    )
    from ..application.operation_store import Operation, OperationError, OperationState, StateConflict, StateStoreError
    from ..application import risk_summary, summary_store
else:  # pytest imports adapters from the add-on root
    from application.errors import ErrorCategory
    from application.model_port import ModelFailure, ModelRequest, ModelResponse
    from application.operation_store import Operation, OperationError, OperationState, StateConflict, StateStoreError
    from application import risk_summary, summary_store

__all__ = [
    "ErrorCategory", "ModelFailure", "ModelRequest", "ModelResponse",
    "Operation", "OperationError", "OperationState", "StateConflict", "StateStoreError",
    "risk_summary", "summary_store",
]
