"""仓库地址校验:SSRF 与凭据注入的第一道闸门。"""

import pytest
from fastapi import HTTPException

from app.api.repo_routes import _validate_repo_url


def test_accepts_public_https_repo_and_cleans_input():
    assert _validate_repo_url("  `https://github.com/psf/requests`  ") == "https://github.com/psf/requests"


def test_accepts_gitee_and_gitlab():
    assert _validate_repo_url("https://gitee.com/mirrors/requests") == "https://gitee.com/mirrors/requests"
    assert _validate_repo_url("https://gitlab.com/gitlab-org/gitlab") == "https://gitlab.com/gitlab-org/gitlab"


@pytest.mark.parametrize(
    "bad_url,reason",
    [
        ("http://github.com/psf/requests", "非 https"),
        ("https://user:token@github.com/psf/requests", "URL 内嵌凭据"),
        ("https://github.com.evil.com/psf/requests", "域名伪装"),
        ("https://evil.com/github.com/psf/requests", "非白名单域名"),
        ("https://github.com/", "缺少仓库路径"),
    ],
)
def test_rejects_unsafe_urls(bad_url, reason):
    with pytest.raises(HTTPException) as exc:
        _validate_repo_url(bad_url)
    assert exc.value.status_code == 400, reason
