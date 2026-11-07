"""pytest 公共配置:路径与占位环境变量。

单元测试不连数据库、不调模型,所以在导入 app 之前先塞入占位配置,
既满足 Settings 的"必填字段"约束,也避免测试意外打到真实 Key。
"""

import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

_DUMMY_ENV = {
    "LLM_API_KEY": "test-key",
    "LLM_BASE_URL": "https://api.deepseek.com",
    "LLM_MODEL": "deepseek-chat",
    "EMBEDDING_API_KEY": "test-key",
    "EMBEDDING_BASE_URL": "https://api.siliconflow.cn/v1",
    "EMBEDDING_MODEL": "BAAI/bge-m3",
    "DATABASE_URL": "postgresql+psycopg://test:test@localhost:5432/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "test-jwt-secret-0123456789abcdef",
    "RERANK_MODEL": "BAAI/bge-reranker-v2-m3",
    "ADMIN_PASSWORD": "test-admin-password",
    "BYOK_ENABLED": "true",
    "BYOK_ALLOWED_HOSTS": "api.deepseek.com,api.siliconflow.cn",
}

for name, value in _DUMMY_ENV.items():
    os.environ.setdefault(name, value)
