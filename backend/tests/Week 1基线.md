# Week 1 检索基线（RAG 测试集）

用途：Week 3 做完混合检索（向量 + BM25 + RRF）与重排后，用同一批问题重跑一遍，
对比命中率变化。这里是**改造前的原始基线**，不要事后修改实测数据。

记录时间：Week 1 收尾（提交 `93ac4fe`）

## 1. 基线配置（复现的前提）

| 项 | 值 |
| --- | --- |
| 语料 | 本仓库 `README.md`，5069 字符 |
| 切分器 | `RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)` |
| 切分结果 | 9 块（去重后仍为 9，无重复） |
| Embedding | 硅基流动 `BAAI/bge-m3`（dim=1024） |
| 向量库 | pgvector，collection 名 `readme` |
| 检索方式 | 纯向量 `store.as_retriever(search_kwargs={"k": 4})` |
| 召回上限 | k=4（top-4） |
| LLM | DeepSeek `deepseek-chat`，`temperature=0` |
| sources 返回 | 每块截断到前 200 字 |

## 2. 测试用例与实测结果

| # | 问题 | 期望 | W1 实测 | 判定 | 证据来源 |
| --- | --- | --- | --- | --- | --- |
| 1 | 这个项目用什么做向量库？ | 提到 pgvector | 回答正确 | 通过 | 用户实测 |
| 2 | 数据库端口为什么用 5433？ | 原生 PostgreSQL 17 占了 5432 | 回答正确 | 通过 | 用户实测 |
| 2b | `5433`（只给一个关键词） | 带出 5433 相关段落 | 答出 `5433` / `pgvector/pgvector:pg16` / `repotalk-db-1`，sources=4 | 通过 | AI 实测（HTTP） |
| 3 | Week 4 用什么做异步任务？ | arq | `资料中未提及。` | **失败（漏召）** | AI 实测，见第 3 节 |
| 4 | 这个项目的创始人是谁？ | 资料中未提及 | `资料中未提及` | 通过（防幻觉） | 用户实测 |
| 5 | collection 填 `abc`（未 ingest）直接 `/chat` | HTTP 409 | 409 | 通过（契约） | 用户实测 |

小结：6 条里 5 条通过，唯一失败的是 **#3 漏召**。

## 3. 失败用例 #3 的完整记录（Week 3 的主要对照目标）

问题：`Week 4 用什么做异步任务？`

**关键前提：语料里确实有 `arq`。**
- `README.md` 的「当前进度」表 W4 行：`| W4 | arq 异步解析 + JWT + Redis 缓存 + SSE 进度推送 | 待开始 |`
- `README.md` 的「技术栈」表：`| 异步任务 | arq | Week 4 引入，全异步生态 |`
- 9 块中**含 `arq` 的共 2 块**

**实测相似度排名**（cosine distance，越小越近；k=4 只取前 4 名）：

| 排名 | 距离 | 含 arq | 片段开头 |
| --- | --- | --- | --- |
| 0 | 0.3874 | no | `# RepoTalk` 头部 |
| 1 | 0.6316 | no | `## 待补充（Week 8 收尾清单）` |
| 2 | 0.6456 | no | `### 2. 准备 .env` |
| 3 | 0.6821 | no | `另一条路（psycopg3 异步）…` |
| — | — | — | ↑ 以上 4 块是实际交给 LLM 的，全都不含 arq |
| 4 | 0.6936 | no | `浏览器打开 /docs …` |
| 5 | 0.7046 | no | `## 目录结构` |
| **6** | **0.7059** | **YES** | `## 当前进度`（W4 行在这里） |
| **7** | **0.7091** | **YES** | `## 技术栈`（异步任务行在这里） |
| 8 | 0.7125 | no | `## 环境注意事项` |

**对照实验（证明失败在检索而非生成）**：把排名第 6 的那块单独作为 context，
沿用同一套 prompt 与同一 LLM，回答是 —— `Week 4 用 arq 做异步任务。`

结论：生成端与防幻觉约束均正常，问题 100% 在**检索召回**。

**根因分析**：
1. 措辞不一致：提问用 `Week 4`，语料进度表里写的是 `W4`；而排名第 0 的头部块含 `Week 1`/`Week 2`，
   靠 "Week" 的**表面字面重叠**被顶到第一，实际与"异步任务"无关。
2. 表格文本嵌入退化：Markdown 表格由大量重复的 `| --- | --- |` 与短单元格构成，
   整块向量被稀释，单个 token 的信号很弱。
3. 区分度几乎为零：第 4~8 名距离挤在 0.6936~0.7125 这 0.02 区间内，排序近似随机。

**Week 3 改进目标**：BM25 能对 `arq` 这个 token 做字面命中，RRF 融合后应进入 top-k，
bge-reranker-v2-m3 再做精细重排。改造后本条应从「漏召」变为「命中」。

## 4. 一个运行时坑（本次实测发现）

`/ingest` 的 `pre_delete_collection=True` 是**先删后建**。在重灌 `readme` 的过程中
并发发起 `/chat`，会看到"半空"的 collection，从而产生随机的错误答案
（本次实测中就出现了一次"资料中未提及"）。

教训：**重灌期间不要并发查询**。等 `/ingest` 返回 `ok` 之后再提问。
（Week 4 接异步任务后，这个窗口会变长，需要靠"索引就绪"状态位来规避。）

## 5. 如何复现排名数据

在 `backend/` 目录下（需要先 `/ingest` 灌好 `readme`）：

```python
from langchain_postgres import PGVector
from langchain_openai import OpenAIEmbeddings
from app.core.config import settings

store = PGVector(
    embeddings=OpenAIEmbeddings(
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
    ),
    collection_name="readme",
    connection=settings.database_url,
)
for rank, (doc, score) in enumerate(
    store.similarity_search_with_score("Week 4 用什么做异步任务？", k=9)
):
    print(rank, round(score, 4), "arq" in doc.page_content, doc.page_content[:40])
```

提问走接口（`/docs` 的 `POST /chat`，或）：

```powershell
$q = @{ question = "Week 4 用什么做异步任务？"; collection = "readme" } | ConvertTo-Json -Compress
Invoke-RestMethod -Uri "http://127.0.0.1:8000/chat" -Method Post `
  -ContentType "application/json; charset=utf-8" -TimeoutSec 120 `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($q))
```

注意：每次服务重启后 `_ingested`（内存集合）会清空，必须先重灌一次 `readme`，
否则 `/chat` 直接返回 409。

## 6. Week 3 待补（目标 30 条）

现在只有 6 条，Week 3 需要扩到 30 条再做命中率统计。建议覆盖这几类：

- **事实类**：语料里明确存在、且措辞与提问一致（应稳定命中）
- **缩写/术语类**：如 `W4`、`arq`、`SSE`、`RRF`（纯向量检索的弱点）
- **表格类**：答案只存在于 Markdown 表格单元格中（纯向量检索的弱点）
- **防幻觉类**：语料中不存在的问题（应回答「资料中未提及」，绝不能编）
- **契约类**：未 ingest 的 collection、过短的 text、非法 collection 名

评分口径建议固定下来（Week 3 前定好，之后不要改）：
命中 = 期望的关键词出现在 `answer` 中，或出现在 `sources` 的片段中。
