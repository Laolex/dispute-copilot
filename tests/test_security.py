import pytest

from app.config import ConfigError, Settings, is_sandbox_url
from app.paypal import PayPalClient

GOOD = "https://api-m.sandbox.paypal.com"


@pytest.mark.parametrize("url", [
    "https://api-m.sandbox.paypal.com.attacker.example",
    "https://attacker.example/api-m.sandbox.paypal.com",
    "https://api-m.sandbox.paypal.com@attacker.example",
    "https://api-m.sandbox.paypal.com:8443",
    "https://user:pw@api-m.sandbox.paypal.com",
    "http://api-m.sandbox.paypal.com",
    "https://api-m.sandbox.paypal.com.",
    "https://api-m.sandbox.paypal.com\\@attacker.example",
    "https://attacker.example#api-m.sandbox.paypal.com",
    "https://api-m.sandbox.paypal.com/v1?x=1",
    "https://api-m.paypal.com",
    "",
])
def test_non_sandbox_urls_are_rejected_everywhere(url, monkeypatch):
    assert not is_sandbox_url(url, base_only=True)
    monkeypatch.setenv("PAYPAL_BASE_URL", url)
    with pytest.raises(ConfigError):
        Settings.from_env(require_credentials=False)
    with pytest.raises(ValueError):
        PayPalClient(Settings("i", "s", url))


def test_real_sandbox_urls_are_accepted():
    assert is_sandbox_url(GOOD, base_only=True) and is_sandbox_url(GOOD + "/", base_only=True)
    assert is_sandbox_url(GOOD + "/v1/customer/disputes/PP-R-X/accept-claim")
    assert is_sandbox_url("https://api-m.sandbox.paypal.com:443/v1/x")
    assert not is_sandbox_url(GOOD + "/v1/x", base_only=True)      # a link is not a base URL
