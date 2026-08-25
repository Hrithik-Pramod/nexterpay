"""Domain errors.

These are raised by the domain layer and translated into user-facing messages
by the bot layer. The domain never formats a Telegram reply itself.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all expected domain failures."""


class UnknownChat(DomainError):
    """A Telegram chat that has not been registered. The bot ignores these."""


class NotAuthorised(DomainError):
    """The acting staff member lacks the required role."""


class AlreadyOwned(DomainError):
    """Work item is already claimed by someone else."""


class WorkItemClosed(DomainError):
    """The work item is closed and cannot be modified without reopening."""


class InvalidTransition(DomainError):
    """The requested status change is not permitted from the current status."""
