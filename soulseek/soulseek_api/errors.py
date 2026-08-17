"""Exception types raised by the Soulseek client."""


class SoulseekError(Exception):
    """Base class for every error raised by this package."""


class ConnectionFailed(SoulseekError):
    """The slskd daemon could not be reached at all."""


class AuthenticationFailed(SoulseekError):
    """Credentials or API key were rejected by slskd."""


class APIError(SoulseekError):
    """slskd returned a non-success HTTP status."""

    def __init__(self, status_code, message, url=None):
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code} from {url or 'slskd'}: {message}")


class SearchTimeout(SoulseekError):
    """A search did not complete within the allotted time."""


class DownloadFailed(SoulseekError):
    """A transfer ended in a failed / cancelled state."""
