"""JWT 签发与过期:签发方与校验方必须共用同一套算法与密钥。"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from jwt.exceptions import ExpiredSignatureError

from app.core.config import settings
from app.core.security import ACCESS_TOKEN_EXPIRE_HOURS, ALGORITHM, create_access_token


def test_token_roundtrip_carries_subject():
    token = create_access_token("admin")
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    assert payload["sub"] == "admin"
    assert "exp" in payload and "iat" in payload


def test_expired_token_is_rejected():
    past = datetime.now(timezone.utc) - timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS + 1)
    token = jwt.encode(
        {"sub": "admin", "iat": past, "exp": past},
        settings.jwt_secret,
        algorithm=ALGORITHM,
    )
    with pytest.raises(ExpiredSignatureError):
        jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
