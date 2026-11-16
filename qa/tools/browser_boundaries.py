"""Pure URL policy for the owned browser context (not an OS firewall)."""
from urllib.parse import urlsplit


def browser_target_allowed(url, profile, *, websocket=False):
    try:
        parts = urlsplit(url)
        return (parts.scheme in ({'ws'} if websocket else {'http'}) and parts.hostname == '127.0.0.1'
                and parts.port in {profile.ui_port, profile.api_port}
                and parts.username is None and parts.password is None)
    except (ValueError, TypeError):
        return False
