import pytest
from app.tools.implementations.web_search_tool import web_search_tool


@pytest.mark.parametrize(
    "malicious_url",
    [
        "http://127.0.0.1/admin",
        "https://10.0.0.5:8080/metrics",
        "http://192.168.1.1/config",
        "http://169.254.169.254/latest/meta-data/",  # AWS Metadata IP
    ],
)
def test_ssrf_blocks_private_ips(malicious_url):
    """
    CRITICAL SECURITY TEST:
    Ensures that the _validate_url_for_ssrf strictly blocks internal network scanning.
    """
    # The method should return False, indicating the URL is invalid/blocked.
    is_valid = web_search_tool._validate_url_for_ssrf(malicious_url)
    assert is_valid is False


@pytest.mark.parametrize(
    "valid_url",
    [
        "https://google.serper.dev/search",
        "https://api.github.com/v3",
        "https://www.google.com",
    ],
)
def test_ssrf_allows_public_ips(valid_url):
    """
    Ensures that legitimate external domains are allowed.
    """
    is_valid = web_search_tool._validate_url_for_ssrf(valid_url)
    assert is_valid is True
