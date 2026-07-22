"""Public error types used by the kernel and CLI."""

from __future__ import annotations


class ContinuityError(Exception):
    """Base class for expected, user-actionable failures."""


class ValidationError(ContinuityError):
    """Stored or supplied data violates the public format."""


class ConflictError(ContinuityError):
    """A compare-and-swap write used a stale revision."""


class NotFoundError(ContinuityError):
    """A requested record does not exist."""


class SetupError(ContinuityError):
    """Installation or integration could not be completed safely."""
