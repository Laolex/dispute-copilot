"""Environment-driven config. Validated at startup; missing required values raise immediately."""
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

SANDBOX_HOST = "api-m.sandbox.paypal.com"


class ConfigError(RuntimeError):
    pass


def is_sandbox_url(url: str, *, base_only: bool = False) -> bool:
    """True only for https://api-m.sandbox.paypal.com[/path]. Parsed, never substring-matched:
    the host must equal the sandbox host exactly, with no credentials, no custom port, no query or fragment.
    base_only additionally requires that there is no path (a base URL, not a link)."""
    try:
        u = urlsplit(url)
        host, port = u.hostname, u.port
    except ValueError:
        return False
    if u.scheme != "https" or host != SANDBOX_HOST or u.username is not None or u.password is not None:
        return False
    if port not in (None, 443) or u.fragment or "\\" in url or any(c.isspace() for c in url):
        return False
    if base_only and (u.path not in ("", "/") or u.query):
        return False
    return True


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    base_url: str = f"https://{SANDBOX_HOST}"
    out_dir: str = "spike/out"

    @staticmethod
    def from_env(require_credentials: bool = True) -> "Settings":
        client_id = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
        client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
        base_url = os.environ.get("PAYPAL_BASE_URL", f"https://{SANDBOX_HOST}").strip().rstrip("/")
        if require_credentials and not (client_id and client_secret):
            raise ConfigError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are required")
        # Hackathon build is sandbox-only. Refuse anything else, no override flag.
        if not is_sandbox_url(base_url, base_only=True):
            raise ConfigError(f"refusing non-sandbox PAYPAL_BASE_URL: {base_url}")
        return Settings(client_id, client_secret, base_url, os.environ.get("SPIKE_OUT_DIR", "spike/out"))
