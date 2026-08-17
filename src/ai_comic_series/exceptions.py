"""Project-specific exception hierarchy."""

__all__ = [
    "AuthenticationError",
    "ComicSeriesError",
    "ConfigurationError",
    "IntegrityError",
    "RemoteExecutionError",
    "RemoteProtocolError",
    "RemoteTimeoutError",
]


class ComicSeriesError(RuntimeError):
    """Base class for actionable pipeline failures."""


class ConfigurationError(ComicSeriesError):
    """Raised when local configuration or process-local credentials are invalid."""


class AuthenticationError(ComicSeriesError):
    """Raised when the AMD/Jupyter session is missing, expired, or unauthorized."""


class RemoteProtocolError(ComicSeriesError):
    """Raised when Jupyter returns a malformed or unexpected response."""


class RemoteExecutionError(ComicSeriesError):
    """Raised when code executed by a remote kernel fails."""


class RemoteTimeoutError(ComicSeriesError):
    """Raised when a bounded remote operation does not complete in time."""


class IntegrityError(ComicSeriesError):
    """Raised when a file size, digest, or generation fingerprint does not match."""
