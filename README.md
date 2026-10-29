<!-- Week 5 checkpoint -->
# RepoTalk

> 开源仓库代码问答 Agent —— 把 GitHub 仓库喂进来，用自然语言提问，得到带出处的答案。

当前状态：**Week 6 已完成**——RAGAS 四指标评测跑通，Docker Compose 五容器部署就绪。

## 这是什么

给定一个开源仓库，你可以直接问"这个项目的鉴权逻辑在哪""XX 函数到底在干什么"，Agent 会检索相关代码片段并**只基于这些片段**回答，同时返回命中的原始片段作为引用来源。

和普通文档问答的区别在切分方式：不按字数硬切，而是按**语法树（AST）**以函数 / 类为单位切块，并保留 `file_path` / `start_line` / `symbol` 元数据。这样检索出来的是完整可用的代码，而不是被拦腰截断的半截函数。

### 数据流

录入（异步，worker 消费队列）：

```mermaid
flowchart TD
    A["POST /repos<br/>用户提交仓库地址"] --> B["FastAPI<br/>创建 Repo 记录 + enqueue_job"]
    B --> C["立即返回 repo_id<br/>（前端拿 repo_id 轮询 / SSE 订阅）"]
    B --> D{{"Redis 队列"}}
    D --> E["arq worker<br/>（独立容器，独立进程）"]
    E --> E1["git clone --depth 1<br/>浅克隆目标仓库"]
    E1 --> E2["tree-sitter AST 切分<br/>按函数 / 类边界切块<br/>保留 file_path / start_line / symbol"]
    E2 --> E3["bge-m3 向量化<br/>写入 PostgreSQL pgvector"]
    E2 --> E4["构建 BM25 索引原料<br/>JSON 序列化写入 Redis<br/>key=bm25:{collection}, TTL=7天"]

    style A fill:#e1f5ff
    style E fill:#fff3e1
```

问答：

```mermaid
flowchart TD
    Q["用户问题<br/>POST /chat 或 /chat/stream"] --> V["向量检索 top-80<br/>pgvector 余弦相似度"]
    Q --> BM["BM25 检索 top-80<br/>内存重建索引（从 Redis 原料）"]
    Q --> EX["精确符号召回<br/>从 query 抽标识符<br/>filter=symbol:{ident}, k=12"]

    V --> F["自写 RRF 倒数排名融合<br/>只看排名不看分数<br/>解决量纲不一致"]
    BM --> F
    EX --> F

    F --> BST["标识符精确加分 +1.0<br/>同 (文件,符号) 最多保留 3 条<br/>总 candidate 上限 30"]
    BST --> RK["bge-reranker-v2-m3 精排<br/>Cross-Encoder 逐对打分<br/>截断 2000 字符 / 块"]
    RK --> TOP["取 top-6<br/>喂给 LLM"]
    TOP --> PR["拼 prompt<br/>约束「仅根据上下文回答」<br/>附带引用来源元数据"]
    PR --> LLM["DeepSeek temperature=0<br/>生成答案"]
    LLM --> OUT["answer + sources<br/>流式走 SSE 逐 token 推送"]

    style Q fill:#e1f5ff
    style F fill:#fff3e1
    style RK fill:#f3e1ff
    style OUT fill:#e1ffe1
```

流式问答走 `POST /chat/stream`：先推引用来源，再逐 token 推回答（SSE）。

### 部署架构（Docker Compose 五容器）

```mermaid
flowchart TD
    subgraph 浏览器
        U["用户浏览器"]
    end

    subgraph Docker Compose
        direction LR
        subgraph 反代层
            N["Nginx :80<br/>前端静态托管 + API 反代<br/>SSE proxy_buffering off"]
        end
        subgraph 应用层
            F["FastAPI + Uvicorn<br/>API 路由 + RAG 流水线 :8000"]
            W["arq worker<br/>异步解析（克隆→切分→向量化）"]
        end
        subgraph 数据层
            DB[("PostgreSQL 16<br/>pgvector<br/>端口 5432")]
            R[("Redis 7<br/>队列 + BM25 原料<br/>端口 6379")]
        end
    end

    U -->|"/api/**"| N
    U -->|"/"| N
    N -->|反代 API| F
    N -->|托管静态文件| U
    F --> DB
    F --> R
    W --> DB
    W --> R

    style N fill:#ffe1e1
    style F fill:#e1f5ff
    style W fill:#e1f5ff
    style DB fill:#fff3e1
    style R fill:#fff3e1
```

一键启动：`docker compose up -d`。生产环境需在 Nginx 前面加 HTTPS 证书（Let's Encrypt）。

## 当前进度

| 周次 | 内容 | 状态 |
| --- | --- | --- |
| W0 | 环境搭建 + FastAPI 骨架 | 已完成 |
| W1 | 最小 RAG 闭环（纯文本） | 已完成 |
| W2 | 代码级切分（tree-sitter AST + 元数据） | 已完成 |
| W3 | 混合检索（向量 + BM25 + 自写 RRF）+ 重排 | 已完成 |
| W4 | arq 异步解析 + JWT + Redis 缓存 + SSE 进度推送 | 已完成 |
| W5 | 前端工作台（Vite + React-TS） | 已完成 |
| W6 | RAGAS 四指标评测 + 部署上线 | 已完成 |
| W7 | 扩展：LLM Wiki 自动文档生成（DeepWiki 模式，可选加分项） | 已规划 |

## 技术栈

| 层 | 选型 | 说明 |
| --- | --- | --- |
| 语言 | Python 3.12 | 不上 3.13+，避免依赖兼容问题 |
| 包管理 | uv | |
| Web 框架 | FastAPI + Uvicorn | |
| LLM 编排 | LangChain（LCEL） | |
| LLM | DeepSeek `deepseek-chat` | `temperature=0`，问答要稳定 |
| Embedding | 硅基流动 `BAAI/bge-m3` | 中文友好，有免费额度 |
| Rerank | `BAAI/bge-reranker-v2-m3` | Week 3 引入 |
| 向量库 | PostgreSQL 16 + pgvector | `pgvector/pgvector:pg16` 镜像 |
| 缓存 | Redis 7 | Week 4 起用于异步任务与缓存 |
| 异步任务 | arq | Week 4 引入，全异步生态 |
| 数据库驱动 | psycopg3（同步模式） | 选型原因见「环境注意事项」 |
| RAG 评测 | RAGAS 0.2.10 + HuggingFace datasets | 上下文精度 / 召回、忠实度、回答相关性 |
| 部署 | Docker Compose + Nginx | 前端静态托管 + API 反代 + SSE 直通 |

## 目录结构

```
repotalk/
├── backend/
│   ├── app/
│   │   ├── api/              # 路由层：health / auth / repos / chat
│   │   ├── core/             # 配置、DB、Redis 缓存、JWT
│   │   ├── models/           # 数据模型（Repo 等）
│   │   ├── rag/              # 切分 / 向量化 / 混合检索 / 生成
│   │   ├── workers/          # arq 异步解析 worker
│   │   └── main.py           # 应用入口
│   ├── tests/
│   │   ├── ragas_dataset.json        # 30 条评测集
│   │   └── test_week6.py             # RAGAS 四指标评测脚本
│   ├── Dockerfile
│   ├── requirements-frozen.txt       # 已验证的精确依赖清单（勿删）
│   ├── .env                          # 密钥文件，绝不提交到 Git
│   └── pyproject.toml
├── frontend/                 # Vite + React-TS 工作台（src/ + dist/）
├── nginx/nginx.conf          # 静态托管 + /api 反代 + SSE 配置
├── docker-compose.yml        # db / redis / backend / worker / nginx
└── README.md
```

分层约定：`api → services → rag`。路由层只做参数校验和响应封装，业务逻辑放 services / rag。

## 本地启动

### 1. 启动依赖服务

```powershell
docker compose up -d db redis
```

只起两个基础容器（本地开发时后端和 worker 跑在宿主机，不需要容器）：

- `repotalk-db-1`：`pgvector/pgvector:pg16`，映射到宿主机 **5433** 端口
- `repotalk-redis-1`：`redis:7-alpine`，映射到宿主机 6379

### 2. 准备 `.env`

在 **`backend/` 目录下**创建 `.env`（位置很关键，见下方注意事项）：

```bash
# LLM：DeepSeek
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# Embedding：硅基流动 BGE-M3
EMBEDDING_API_KEY=sk-xxxxxxxxxxxxxxxx
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3

# 数据库：注意端口是 5433
DATABASE_URL=postgresql+psycopg://repotalk:repotalk@localhost:5433/repotalk

# 缓存
REDIS_URL=redis://localhost:6379/0

# JWT 密钥（Week 4 用，请用长随机字符串）
JWT_SECRET=请替换为一串长随机字符
```

### 3. 装依赖并启动

> **不要执行 `uv sync` / `uv lock`**。`ragas 0.2.10` 依赖 `langchain-community 0.3.x`，而 `langchain>=1.4.6` + `langchain-postgres 0.0.19` 要求 `langchain-core 1.x`，两边无解，`uv lock` 必然失败。依赖统一按已验证的冻结清单安装。

```powershell
cd backend
uv pip install -r requirements-frozen.txt
```

装完后验证关键依赖可用：

```powershell
.\.venv\Scripts\python.exe -c "from ragas import evaluate; print('ragas OK')"
```

启动后端（终端 1）：

```powershell
uv run uvicorn app.main:app --reload --port 8000
```

启动异步解析 worker（终端 2，**必须起，否则上传仓库后进度不会动**）：

```powershell
uv run arq app.workers.parse_worker.WorkerSettings
```

启动前端开发服务器（终端 3）：

```powershell
cd frontend
npm install
npm run dev
```

浏览器打开 <http://127.0.0.1:5173>（前端，`/api` 由 Vite 代理到 8000）。接口文档在 <http://127.0.0.1:8000/docs>。

> `uvicorn` 和 `arq` 都需要各自独占一个终端并保持运行。在同一个终端里再敲别的命令会把它们终止掉。

## 接口

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| GET | `/health` | 健康检查，返回 `{"status": "ok"}` | 无 |
| POST | `/auth/login` | 用管理员密码换 JWT | 无 |
| POST | `/repos` | 创建仓库解析任务，立即返回 `repo_id` | 无 |
| GET | `/repos` | 查询所有仓库状态 | 无 |
| GET | `/repos/{repo_id}` | 查询单个仓库状态 | 无 |
| GET | `/repos/{repo_id}/events` | SSE 订阅解析进度 | 无 |
| POST | `/chat` | 混合检索 + 生成回答，返回 `answer` + `sources` | Bearer |
| POST | `/chat/stream` | SSE 流式问答：先推来源，再逐 token 推回答 | Bearer |

请求示例：

```json
// POST /auth/login
{ "password": "ADMIN_PASSWORD 的值" }

// POST /repos
{ "repo_url": "https://github.com/psf/requests", "collection": "requests" }

// POST /chat
{ "repo_id": 1, "question": "request 函数在哪个文件定义？" }
```

`repo_id` 与 `collection` 二选一，优先用 `repo_id`（会自动查表拿到 collection 并校验解析状态）。
仓库状态流转：`pending → parsing → done / failed`，只有 `done` 才能提问。

`/chat` 响应（注意字段名是单数 `source`）：

```json
{
  "answer": "request 函数在 api.py 文件中定义。",
  "source": [
    { "file_path": "src/requests/api.py", "start_line": 14, "end_line": 71,
      "symbol": "request", "content": "命中的原始代码片段（前 200 字）" }
  ]
}
```

`/chat/stream` 走 SSE，消息依次是：

```
data:{"type":"status","stage":"retrieving"}
data:{"type":"sources","sources":[...]}   ← 复数 sources
data:{"type":"token","content":"..."}      ← 多次
data:{"type":"done"}                        ← 或 {"type":"error","message":"..."}
```

行为约定：

- 检索不到可用上下文时，`answer` 返回「资料中未提及」
- 这是 prompt 里刻意加的防幻觉约束，要求模型只依据检索到的代码回答
- `/chat` 与 `/chat/stream` 会真实消耗 DeepSeek / 硅基流动额度，所以必须带 JWT

## 生产部署

```powershell
# 1. 构建前端静态文件（产物 frontend/dist 会被 Nginx 挂载）
cd frontend
npm run build
cd ..

# 2. 构建后端镜像并启动全部服务
docker compose build backend
docker compose up -d
```

启动后应有 **5 个容器**：

| 服务 | 作用 |
| --- | --- |
| `db` | `pgvector/pgvector:pg16`，宿主机映射 5433 |
| `redis` | Redis 7，任务队列 + 检索缓存 |
| `backend` | uvicorn 跑 FastAPI，容器内 8000 |
| `worker` | arq 消费解析任务（**缺了它上传仓库会一直卡住**） |
| `nginx` | 托管前端静态文件 + `/api` 反代，对外 80 |

| 地址 | 内容 |
| --- | --- |
| <http://localhost> | 前端工作台 |
| <http://localhost/api/health> | 经 Nginx 反代的后端健康检查 |
| <http://localhost:8000/docs> | FastAPI 文档（直连，调试用） |

部署后逐条验证：

```powershell
(Invoke-WebRequest "http://localhost:8000/health" -UseBasicParsing).StatusCode  # 期望 200
(Invoke-WebRequest "http://localhost/api/health" -UseBasicParsing).StatusCode  # 期望 200，验证反代链路
(Invoke-WebRequest "http://localhost" -UseBasicParsing).StatusCode             # 期望 200，验证前端
```

Nginx 里 SSE 相关的三项配置不能删，否则流式进度会失效：

- `proxy_buffering off` —— 不缓冲，后端产生一条就转发一条
- `proxy_http_version 1.1` —— HTTP/1.0 不支持长连接
- `proxy_read_timeout 300s` —— 长连接空闲时间放宽

常见故障对照：

| 现象 | 排查 |
| --- | --- |
| `docker compose build backend` 失败 | 看构建日志末尾，多为网络拉包失败 |
| backend 一直 `starting` | `docker compose logs backend --tail 100` |
| 访问 80 返回 502 | `docker compose logs nginx`，检查 upstream 是否为 `server backend:8000` |
| 前端 403 / 404 | `docker compose exec nginx ls /usr/share/nginx/html`，确认 `frontend/dist` 已构建 |
| 上传仓库进度不动 | `docker compose logs worker --tail 50`，确认 worker 在跑且 Redis 可达 |

## RAGAS 评测

评测集 `backend/tests/ragas_dataset.json` 共 30 条，覆盖三类问题：函数定位、类定位、参数用法。每条都带人工写的 `ground_truth` 与 `ground_truth_contexts`。

运行方式（需 db / redis 已启动，且 requests 仓库已入库）：

```powershell
cd backend
uv run python -m tests.test_week6
```

单次约 10–12 分钟，其中 RAG 流水线约 2 分钟，LLM 裁判打分约 9 分钟。

| 指标 | 含义 | 调优前 | 调优后 | 涨幅 |
| --- | --- | --- | --- | --- |
| `context_precision` | 检索块中有多少真正相关 | 27.2% | **76.9%** | +49.7% |
| `context_recall` | 标准答案需要的信息有多少被检索到 | 18.9% | **92.2%** | +73.3% |
| `faithfulness` | 回答的每句是否都能在上下文中找到依据 | 28.8% | **93.3%** | +64.5% |
| `answer_relevancy` | 回答是否切题 | 74.4% | 72.5% | -1.9% |

**全部跨过 0.6 及格线** ✅。调优方法：通过 7 个参数联动提升检索管线——粗筛候选数 `CANDIDATE_K` 从 50→80、精排保留数 `RERANK_TOP_N` 从 4→6、rerank 截断从 1400→2000 字符、同 (文件,符号) 保留上限从 2→3、精确标识符加分从 0.5→1.0、exact 精确召回每符号 k=8→12、总 candidate 上限 20→30。平均提升 **+52 个百分点**，证明瓶颈确实在检索而非生成层。

> 关键洞察：answer_relevancy 在调优前就有 74.4%（生成层本身会切题），调优后反而微降 1.9%——这是正常的，因为喂给 LLM 的上下文从 4 块变成 6 块，信息更全但也带来更多噪音。context_recall 从 18.9% 涨到 92.2% 才是核心：之前答案块根本排不进前 4 被丢掉了。

完整报告落在 `backend/tests/week6_ragas_report.json`，含每条样本的回答与检索上下文，便于逐条复盘。

## 环境注意事项（踩坑记录）

这几条都是实际踩出来的，换机器或重装环境时会重复遇到。

**1. 数据库端口用 5433，不是 5432**
本机装了原生 PostgreSQL 17（`D:\PostgreSQL\17`，服务名 `postgresql-x64-17`），它占用了宿主机 5432，会把 Docker 容器的端口映射挤掉。此时从宿主机连 5432 实际打到的是原生 PG，会报出极具误导性的 `用户 "repotalk" 密码认证失败`（因为原生 PG 里根本没有 repotalk 这个角色）。因此 `docker-compose.yml` 里映射写成 `"5433:5432"`，`.env` 的 `DATABASE_URL` 也必须是 5433。

**2. 数据库驱动用 psycopg3 同步模式，不用 asyncpg**
`langchain_postgres` 的 `acreate_vector_extension` 会把这样一条**多语句 SQL** 发给数据库：

```sql
SELECT pg_advisory_xact_lock(...);CREATE EXTENSION IF NOT EXISTS vector;
```

asyncpg 走预处理语句协议，协议层不允许一条语句里有多条命令，直接报 `cannot insert multiple commands into a prepared statement`——与扩展是否已安装无关。

另一条路（psycopg3 异步）在 Windows 上也走不通：`Psycopg cannot use the 'ProactorEventLoop'`。

所以 `DATABASE_URL` 用 `postgresql+psycopg://`，`PGVector` 走默认的同步模式，`build_index` / `answer` 也保持同步写法。异步化推迟到 Week 3/4 作为一次专门重构。

**3. `.env` 必须放在 `backend/` 目录**
pydantic-settings 的 `env_file` 是相对 **当前工作目录（CWD）** 解析的，而 `uvicorn` 一律从 `backend/` 启动。放在仓库根目录会读不到文件，然后抛出一堆字段缺失的 `ValidationError`。

**4. `.env` 绝对不进 Git**
已在 `.gitignore` 里排除。提交前用 `git status` 确认暂存区没有 `.env`。

**5. 文件名统一小写**
Windows 文件名大小写不敏感，Linux 敏感。`PipeLine.py` 这种写法在本地能跑，部署到 Linux 会直接 `ModuleNotFoundError`。

**6. 接口函数用 `def`，不用 `async def`**
`build_index` / `answer` 内部是同步阻塞调用（含阻塞的 HTTP 请求）。写成 `async def` 会阻塞整个事件循环，连 `/health` 都会被卡住；写成普通 `def` 时 FastAPI 会自动丢到线程池执行。

**7. 依赖已被冻结清单锁定，不要跑 `uv sync` / `uv lock`**
`ragas 0.2.10` 依赖 `langchain-community 0.3.27`（要求 `langchain-core < 1.0`），而 `langchain>=1.4.6` 与 `langchain-postgres 0.0.19` 要求 `langchain-core >= 1.2.11`。两者不可能同时满足，`uv lock` 会直接报 `unsatisfiable`，`uv sync` 也会在失败前改动 venv。

因此当前环境用 `backend/requirements-frozen.txt` 固定（138 个包，已实测跑通 RAGAS 评测）：

```powershell
uv pip install -r requirements-frozen.txt
```

需要新增依赖时用 `uv pip install 包名`，装完刷新清单：

```powershell
uv pip freeze | Out-File -Encoding utf8 requirements-frozen.txt
```

若遇到 `Failed to initialize cache at D:\uv\cache: 拒绝访问`，先改缓存目录：

```powershell
$env:UV_CACHE_DIR="d:\RepoTalk开源仓库代码问答Agent项目\.uv-cache"
```

## 规划中的扩展：LLM Wiki（Week 7 · 可选）

主线完成后，计划把 RepoTalk 从「被动问答」升级为「主动讲解」：对已入库的仓库**自动生成一套带源码引用的 wiki**（即 DeepWiki 模式，参考 [deepwiki.com](https://deepwiki.com) 与开源实现 [deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open)）。

生成流程四步：

1. **结构树** —— 扫描 Week 2 入库的 `file_path` / `symbol` 元数据，聚合成目录树 + 模块清单
2. **大纲规划** —— 让 LLM 根据模块清单产出页面规划：1 个总览页 + N 个模块页
3. **分页生成** —— 每页一个 prompt：该模块的全部 chunk + 写作约束（结论必须标注 `file_path:start_line`，模块关系输出 Mermaid 图）
4. **展示与缓存** —— 前端新增 Wiki 视图（引用可点击跳转），生成结果落库 + Redis 缓存

该扩展**不引入任何新中间件**：复用 Week 2 的切分与元数据、Week 4 的 arq 异步任务与 Redis、Week 5 的前端界面。前置红线：Week 6 公网 URL 上线之前不启动本扩展。

## 待补充（Week 6 收尾 + Week 8 最终交付）

以下是对照项目指导手册 Week 6 · 评测·部署·包装 完整清单后的实际落地情况：

### ✅ 已完成的 Week 6 交付物
- ✔ RAGAS 四指标评测跑通（30 条评测集 + 报告落盘）
- ✔ Docker Compose 五容器（db / redis / backend / worker / nginx）
- ✔ 后端 Dockerfile（多阶段构建）
- ✔ Nginx 反代配置（SSE 三项关键配置齐备）
- ✔ 一键部署脚本 `deploy.psl`
- ✔ 架构图（Mermaid，见本文件「数据流」「部署架构」两节）

### ❌ 未完成的 Week 6 收尾项
- [ ] 公网 HTTPS 域名上线（免费域名 + Let's Encrypt 证书，能直接发给面试官打开）
- [ ] 演示 GIF / 视频（60 秒：登录 → 提交仓库 → 看进度 → 提问 → 流式出答案 → 点引用跳转）
- [ ] 简历 3 条 STAR bullet（参考模板：「基于 AST 切分 + BM25/向量混合检索 + RRF 融合 + Rerank 精排，将代码问答的 context_recall 从 18.9% 提升到 92.2%（平均 +52pt）」）
- [ ] 白板 20 分钟技术讲解准备（面试终极验收）

### 🔧 评测调优（已完成 ✅）
- ✔ `CANDIDATE_K` 50→80、`RERANK_TOP_N` 4→6、rerank 截断 1400→2000、同符号上限 2→3、精确加分 +0.5→+1.0、exact k=8→12、candidates 上限 20→30
- ✔ 四指标全部跨过 0.6 及格线，平均 +52pt
