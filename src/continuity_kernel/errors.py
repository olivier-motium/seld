"""Public error types used by the kernel and CLI."""

from __future__ import annotations

from typing import Literal


class ContinuityError(Exception):
    """Base class for expected, user-actionable failures."""

    provider_authorization_may_remain = False


def mark_provider_authorization_may_remain(error: BaseException) -> None:
    """Annotate a failure only after provider authorization could have occurred."""

    error.__dict__["provider_authorization_may_remain"] = True


class ValidationError(ContinuityError):
    """Stored or supplied data violates the public format."""


OAuthPermissionGrantReason = Literal[
    "missing_selected_permissions",
    "outside_selected_tier",
]


class OAuthPermissionGrantError(ValidationError):
    """The provider's OAuth grant does not match the access the person selected."""

    def __init__(self, reason: OAuthPermissionGrantReason) -> None:
        messages = {
            "missing_selected_permissions": (
                "OAuth provider approved fewer permissions than the selected access"
            ),
            "outside_selected_tier": (
                "OAuth provider returned permissions outside the selected access tier"
            ),
        }
        self.reason = reason
        super().__init__(messages[reason])


class ConflictError(ContinuityError):
    """A compare-and-swap write used a stale revision."""


class NotFoundError(ContinuityError):
    """A requested record does not exist."""


class PersistenceError(ContinuityError):
    """A mutation could not return an ordinary success after persistence work."""


class MutationCommittedError(PersistenceError):
    """A mutation committed, but a later persistence cleanup step failed."""


class DegradedIntegrityError(PersistenceError):
    """A persistence failure could not be rolled back to a known-good state."""


class SetupError(ContinuityError):
    """Installation or integration could not be completed safely."""
