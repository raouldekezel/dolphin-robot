class LoginError(Exception):
    def __init__(self, message: str = "Failed to login"):
        super().__init__(message)
        self.error = message


class TransientAuthError(LoginError):
    """Raised by the auth path when the failure is presumed retryable.

    Covers network faults (DNS timeout, TCP/TLS, socket errors), request
    timeouts, and Cognito responses with 5xx/429 status. Also raised as
    the fail-safe default for unknown exceptions: wiping stored
    credentials is destructive and must be gated on positive evidence of
    server-side rejection, not on our uncertainty (BUG-23).

    Subclass of :class:`LoginError` so callers that only catch
    ``LoginError`` (e.g. ``flow_manager`` config/reauth steps) keep
    handling it gracefully. Callers that must distinguish transient from
    terminal failures — currently ``RestAPI._ensure_id_token_valid`` —
    catch ``TransientAuthError`` before ``LoginError`` and skip
    ``reset_login_details()``.
    """

    def __init__(self, message: str = "Transient auth failure"):
        super().__init__(message)
