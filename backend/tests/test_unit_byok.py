"""BYOK 凭证校验:HTTPS、白名单、控制字符、长度上限。"""

import pytest

from app.core.byok import ByokError, _has_control_chars, allowed_hosts, parse_credentials


def test_allowed_hosts_is_normalized_set():
    hosts = allowed_hosts()
    assert "api.deepseek.com" in hosts
    assert all(h == h.lower() for h in hosts)


def test_valid_credentials_are_stripped():
    cred = parse_credentials(" https://api.deepseek.com/v1/ ", " sk-test ", " deepseek-chat ")
    assert cred.base_url == "https://api.deepseek.com/v1"
    assert cred.api_key == "sk-test"
    assert cred.model == "deepseek-chat"


def test_control_chars_detected():
    assert _has_control_chars("sk-abc\r\nX-Admin: true")
    assert _has_control_chars("tab\there")
    assert not _has_control_chars("sk-normal-key")


@pytest.mark.parametrize(
    "base_url,key,model",
    [
        ("", "sk-test", "deepseek-chat"),
        ("https://api.deepseek.com", "", "deepseek-chat"),
        ("https://api.deepseek.com", "sk-test", ""),
        ("http://api.deepseek.com", "sk-test", "deepseek-chat"),
        ("https://api.evil.com", "sk-test", "deepseek-chat"),
        ("https://user:pass@api.deepseek.com", "sk-test", "deepseek-chat"),
        ("https://api.deepseek.com", "sk-bad\r\nInjected: 1", "deepseek-chat"),
        ("https://api.deepseek.com", "sk-" + "x" * 5000, "deepseek-chat"),
    ],
)
def test_invalid_credentials_rejected(base_url, key, model):
    with pytest.raises(ByokError):
        parse_credentials(base_url, key, model)
