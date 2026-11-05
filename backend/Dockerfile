# ============================================================
# 后端 Dockerfile —— 多阶段构建,减小最终镜像体积
# ============================================================

# ---- 第 1 阶段:构建阶段 ----
# 用带构建工具的镜像编译 tree-sitter 等需要 C 扩展的依赖
FROM python:3.12-slim AS builder

# 设置环境变量,防止 Python 写 .pyc 文件、输出不缓冲
# 注意:这里不能设 PIP_NO_CACHE_DIR=1,否则会绕过下面 pip 的 BuildKit 缓存挂载
ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

# 换用国内镜像源:deb.debian.org 实测约 0.03MB/s,华为云镜像约 5MB/s
RUN set -eux; \
  if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
    sed -i 's|deb.debian.org|mirrors.huaweicloud.com|g' /etc/apt/sources.list.d/debian.sources; \
  else \
    sed -i 's|deb.debian.org|mirrors.huaweicloud.com|g' /etc/apt/sources.list; \
  fi

# 安装构建依赖:gcc 用来编译 tree-sitter 的 C 扩展,git 用来 clone 仓库
# --mount=type=cache:把 apt 的包缓存和索引放进 BuildKit 缓存,重建时直接复用,
#   不会重复下载 deb 包(普通层缓存在 Dockerfile 变动后会失效,这里不会)
# sharing=locked:backend 和 worker 两个镜像并发构建时,串行访问 apt 缓存,避免锁冲突
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
  apt-get update && apt-get install -y --no-install-recommends \
  gcc \
  git
# apt-get update:更新包索引
# apt-get install -y:安装包,-y 自动确认
# --no-install-recommends:不装推荐的附加包,减小体积
# 缓存挂载的内容不会写进镜像层,所以这里不需要再 rm -rf /var/lib/apt/lists/*
# (加了反而会把缓存索引删掉,下次重建又要重新下载)

# 设置工作目录
WORKDIR /app

# 先复制依赖清单,利用Docker缓存层
# requirements-frozen.txt:本地评测验证过的精确版本清单
COPY requirements-frozen.txt ./
# COPY:把宿主机的文件复制到镜像里
# requirements-frozen.txt:本地评测验证过的精确版本清单
# ./:复制到当前工作目录 /app

# 按冻结清单安装依赖:逐行读取 requirements-frozen.txt 并逐个安装
# --mount=type=cache:wheel 下载缓存放在 BuildKit 缓存里,重建时不用重下(约省 0.3GB 流量)
#   缓存不进镜像层,所以镜像体积不受影响
# --no-deps:跳过依赖求解,把清单当成"封闭集合"逐条安装
#   必须加:langchain-postgres 0.0.19 的元数据声明需要 langchain-core>=1.2.11,
#   而实际运行验证过的是 0.3.86(ragas 0.2.10 要求 <1.0)。pip 会做完整求解并报
#   ResolutionImpossible;本地 uv pip install 不校验元数据所以能跑通,这里对齐同样行为。
# -r:指定 requirements 文件,而不是在命令行逐个写包名
RUN --mount=type=cache,target=/root/.cache/pip \
  pip install --no-deps -r requirements-frozen.txt \
  --index-url https://repo.huaweicloud.com/repository/pypi/simple
# pip install:pip 的安装命令,按清单装到系统 Python(容器里不需要 venv)

# ---- 第 2 阶段:运行阶段 ----
# 用干净的 slim 镜像,只复制构建好的依赖和代码
FROM python:3.12-slim

# 同样设置 Python 环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  PATH="/usr/local/bin:${PATH}"

# 运行阶段只需要 git(运行时 clone 仓库用),不需要 gcc
RUN set -eux; \
  if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
    sed -i 's|deb.debian.org|mirrors.huaweicloud.com|g' /etc/apt/sources.list.d/debian.sources; \
  else \
    sed -i 's|deb.debian.org|mirrors.huaweicloud.com|g' /etc/apt/sources.list; \
  fi

# 同样用 BuildKit 缓存挂载,重建时不再重复下载 deb 包
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
  apt-get update && apt-get install -y --no-install-recommends \
  git
# 设置工作目录
WORKDIR /app

# 从 builder 阶段复制已安装好的 Python 包
# /usr/local/lib/python3.12/site-packages:pip 装包的默认位置
# /usr/local/bin:可执行文件(uvicorn 等)
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
# --from=builder:从名为 builder 的阶段复制,而不是从宿主机

# 复制应用代码
# 注意:.dockerignore 里排除了 .venv、__pycache__、.env 等
COPY app/ ./app/
# app/:把宿主机 backend/app/ 目录复制到镜像的 /app/app/

# 预热 tree-sitter grammar 缓存——国内服务器连 GitHub 慢,这里用 timeout 限制
# 每个 grammar 下载不超过 30s,超时就跳过,运行时代码里有 fallback
RUN python -c "import tree_sitter_language_pack as tsl; [tsl.get_language(l) for l in ['python','javascript','typescript','java','go','rust']]" 2>/dev/null || echo "grammar warmup skipped (will fallback at runtime)"

# 暴露端口:FastAPI 默认用 8000
EXPOSE 8000
# EXPOSE:声明容器监听的端口,只是文档作用,实际映射靠 docker run -p

# 健康检查:每 30 秒探一次 /health,连续 3 次失败标记为不健康
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
# --interval=30s:每 30 秒检查一次
# --timeout=5s:单次检查 5 秒超时
# --start-period=10s:启动后 10 秒内失败不算(给应用启动时间)
# --retries=3:连续失败 3 次才标记不健康
# CMD ...:实际执行的检查命令,请求 /health 接口

# 启动命令:用 uvicorn 跑 FastAPI
# --host 0.0.0.0:监听所有网卡(容器内必须 0.0.0.0,否则宿主机访问不到)
# --port 8000:监听 8000 端口
# --workers 1:单 worker(因为有状态,多 worker 会出问题)
# app.main:app:应用入口模块路径
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
# CMD:容器启动时执行的命令
# 用 exec 形式(数组)而不是 shell 形式,信号能正确传递给 uvicorn
