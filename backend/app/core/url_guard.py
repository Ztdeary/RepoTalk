"""仓库地址白名单校验。

单独成模块的原因:这是纯字符串/URL 逻辑,不依赖向量库与框架业务,
放在 core 下可以被 API 层和单测直接复用——单测因此不必安装 langchain 全家桶。
"""

from urllib.parse import urlparse

from fastapi import HTTPException

# 允许被 clone 的代码托管站域名。
# 为什么必须限制:repo_url 是访客可控输入,worker 会拿它执行 git clone;
# 不限制的话,访客填 http://169.254.169.254/... 之类,就能让我们去访问云厂商元数据接口
ALLOWED_REPO_HOSTS = {"github.com", "gitee.com", "gitlab.com"}


def validate_repo_url(raw: str) -> str:
    """校验并清洗仓库地址:必须 https + 白名单域名 + 不带账号密码。

    返回清洗后的地址(去空格/反引号/尾斜杠);不合法直接抛 400。
    """
    url = raw.strip().strip("`").strip()  # 三级清理:去空格->去反引号->再去空格
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="仓库地址必须以 https:// 开头")
    if parsed.username or parsed.password:
        # https://user:token@github.com/... 这种写法会把凭据带进 git 命令,
        # 既可能泄漏,也会绕过"只看域名"的白名单判断
        raise HTTPException(status_code=400, detail="仓库地址不能包含用户名或密码")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_REPO_HOSTS:
        raise HTTPException(
            status_code=400,
            detail=f"目前只支持 {', '.join(sorted(ALLOWED_REPO_HOSTS))} 的公开仓库",
        )
    if not parsed.path or parsed.path == "/":
        raise HTTPException(status_code=400, detail="仓库地址缺少仓库路径")
    return url
