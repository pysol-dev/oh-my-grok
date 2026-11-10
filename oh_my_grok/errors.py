"""Domain errors with stable codes (shared by CLI exit codes and MCP errors)."""
from __future__ import annotations


class OmgError(Exception):
    code = "error"
    exit_code = 1

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(OmgError):
    code = "invalid_input"
    exit_code = 2


class NotFound(OmgError):
    code = "not_found"
    exit_code = 3


class Conflict(OmgError):
    """Stale dispatch/lease/attempt, illegal transition, CAS failure."""
    code = "conflict"
    exit_code = 4


class GateRefused(OmgError):
    """Approval/verification/gate refused."""
    code = "gate_refused"
    exit_code = 5


class IntegrityError_(OmgError):
    """Artifact corruption, hash mismatch, unsafe path."""
    code = "integrity"
    exit_code = 6


# Pre-rename import name. Same class; callers that still say OmoError keep working.
OmoError = OmgError
