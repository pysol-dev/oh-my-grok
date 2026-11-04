"""Domain errors with stable codes (shared by CLI exit codes and MCP errors)."""
from __future__ import annotations


class OmoError(Exception):
    code = "error"
    exit_code = 1

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(OmoError):
    code = "invalid_input"
    exit_code = 2


class NotFound(OmoError):
    code = "not_found"
    exit_code = 3


class Conflict(OmoError):
    """Stale dispatch/lease/attempt, illegal transition, CAS failure."""
    code = "conflict"
    exit_code = 4


class GateRefused(OmoError):
    """Approval/verification/gate refused."""
    code = "gate_refused"
    exit_code = 5


class IntegrityError_(OmoError):
    """Artifact corruption, hash mismatch, unsafe path."""
    code = "integrity"
    exit_code = 6
