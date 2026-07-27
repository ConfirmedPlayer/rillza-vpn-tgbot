"""Typed errors for the CELERITY panel API.

The panel answers with ``{"error": "..."}`` and Russian messages; these
classes let callers branch on the situation instead of parsing text.
"""


class PanelError(Exception):
    """Base class for every panel failure."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class PanelAuthError(PanelError):
    """401 — the API key is wrong, expired or disabled."""


class PanelForbiddenError(PanelError):
    """403 — the key lacks a scope, or its IP allowlist rejected us."""


class PanelNotFoundError(PanelError):
    """404 — no such user, group or subscription token."""


class PanelConflictError(PanelError):
    """409 — the user already exists (the panel returns it in the body)."""


class PanelRateLimitedError(PanelError):
    """429 — per-key rate limit; ``retry_after`` is seconds when known."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status)
        self.retry_after = retry_after


class PanelUnavailableError(PanelError):
    """5xx, a timeout or a connection failure — retryable."""
