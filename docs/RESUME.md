# 简历与面试材料

## 简历项目条目（5 条，建议全用，按岗位取 3–4 条）

**RepoTalk｜开源仓库代码问答系统（个人项目）**

- 基于 tree-sitter 按函数/类边界切块并保留 `file_path`/`start_line`/`symbol`，解决固定窗口切碎函数、答案无法定位的问题；排查并修复 grammar 探测在 Linux 恒为 False 的跨平台缺陷，切块数 **852 → 289**、语法残缺块占比 **28% → 0**。
- 设计四路召回（向量 + BM25 + 符号精确匹配 + 逐字召回）+ 自写 RRF 融合 + rerank 精排：在 36 个源文件 / 1.1 万行的 requests、30 题定位基准上，Recall@6 由纯向量的 **3.3%** 提升到 **98.3%**（命中题数 **1/30 → 29.5/30**）、MRR **0.017 → 0.983**，单题证据检索 **P50 1.8s / P95 2.2s**；消融显示收益来自混合召回而非向量模型，rerank 只补 3.3pp、代价约 +1s/题（零 LLM 成本、脚本可复现）。
- 用 RAGAS 四指标驱动检索参数联动调优：`context_recall` **18.9% → 92.2%**、`faithfulness` **28.8% → 93.3%**、`context_precision` **27.2% → 76.9%**，平均 **+52pt**；据此判定瓶颈在检索层而非生成层，把优化重点从"换更强的 LLM"转到召回与切分。
- 实现多轮上下文记忆：前端只带最近 3 轮、后端按 6 条 / 200 字硬约束裁剪；对指代型追问先规则判定（追问 **10/10** 触发、单轮 **0/30** 不触发）再走 LLM 查询改写（3.73s/次，超时回退原句），追问检索 Recall@6 由 **40% → 100%**、MRR **0.183 → 0.858**（人工改写上限 1.000），历史注入仅增加 **7–10 个 prompt token**。
- 用 arq + Redis 异步解析，SSE 先推引用再逐 token 输出、检索期发心跳防代理断连；实现结果缓存、访客配额、BYOK 域名白名单与 rerank 失败降级；Docker Compose 五容器部署，**52 个纯单测** + GitHub Actions（ruff / pytest / 前端构建）全绿。
- 规则前置、能不用 LLM 就不用：定位类问题先校验检索块里是否真有该符号的定义，没有就直接回「仓库中未找到」，跳过整条生成链路（零 token、零生成等待，只付一次检索 P50 1.79s）；追问改写先用关键词/长度规则判定（单轮 **0/30** 触发、指代追问 **10/10** 触发），首轮不额外付 **3.73s/次** 的改写延迟。
- 检索复用与状态持久化：同问题命中缓存后跳过向量 + BM25 + rerank 全链路，重复提问不再重复付 1.8s 级检索；BM25 索引按「内存 → Redis → 无」三级查找，Redis 开 AOF 并挂数据卷，重启/重建容器后索引与看板统计不再丢失，省掉每次重启后重新解析整仓（500+ 块、分钟级）的等待。

**技术栈（按层）**：后端 Python 3.12 / FastAPI / arq / Redis / LangChain LCEL / SQLAlchemy；检索 PostgreSQL 16 + pgvector / bge-m3 / bge-reranker-v2-m3 / rank-bm25 + jieba / tree-sitter；生成 DeepSeek `deepseek-chat`（temperature=0，支持 BYOK 兼容 OpenAI 接口）；前端 Vite + React + TypeScript；交付 Docker Compose（db / redis / backend / worker / nginx）、Nginx、pytest、ruff、GitHub Actions。

## 专业技能

- **开发语言**：Python、TypeScript、JavaScript、HTML5、CSS3
- **后端开发**：FastAPI、SSE、JWT 鉴权、httpx、uv、pydantic、防注入
- **前端开发**：React、Vite、TailwindCSS、SSE 逐字渲染、代码高亮、引用跳转
- **数据与存储**：PostgreSQL、pgvector、Redis、Embedding、异步任务队列
- **RAG 检索**：AST 语义切分、向量检索、BM25、混合检索、RRF 融合、Rerank 精排、元数据回填
- **LLM 应用**：Prompt 工程、上下文管理、多轮记忆、结构化输出、Function Calling、注入防护
- **Agent 框架**：LangGraph、ReAct、MCP Server、多 Agent 协作
- **评测与质量**：RAGAS、Recall@k、MRR、nDCG、pytest、Ruff、GitHub Actions
- **工程化与部署**：Docker、docker-compose、Nginx、HTTPS、Linux、Git、云服务器部署

> 措辞注意：不要写「Agent」。当前是**RAG 问答链路 + 多轮上下文（历史透传 + 指代改写）**，没有 tool calling / planning / 多跳。写 Agent 一定会被追问，答不上反而扣分。

## 30 秒讲法

1. **一句话**：把 GitHub 仓库按 AST 切成函数块，用多路召回 + RRF + rerank 找到相关代码，再让 LLM 只依据这些代码回答并给出文件行号。
2. **一个数字**：纯向量召回 3.3%，混合召回 98.3% —— 这就是我做这个项目最核心的发现。
3. **一个取舍**：rerank 只多赚 3 个点，但延迟翻倍；我知道它值不值，以及什么场景该关掉。

## 大概率被追问的 6 个问题（准备好答案）

1. **为什么用 RRF 而不是分数加权？**
   两路分数量纲不同（余弦距离 vs BM25 分），加权系数不可迁移；RRF 只看名次，换数据集不用重调。
2. **rerank 只提升 3pp，为什么还留着？**
   提升的是排序而不是召回：把定义块从第 2 名提到第 1 名，减少下游上下文噪声；延迟敏感场景可以按题型关掉。
3. **为什么不做 Agent / 多跳？**
   当前问题类型（定位、用法）单跳足够；加状态机没有收益。要做的话第一步是"先定位文件、再定位符号"的两跳检索。
4. **指标怎么算的？口径有什么问题？**
   文件级二值相关性（评测集只标到文件），先按文件去重防刷分；30 题、单仓库，统计噪声大；只衡量检索，不代表最终回答质量。
5. **单实例怎么扩容？**
   现在 BM25 在进程内存、`--workers 1`，不能多副本。第一步把 BM25 原料统一放 Redis/独立检索服务，再做索引版本化与原子切换。
6. **成本怎么控？**
   演示模式限 4 块上下文 + 320 输出 token，访客每日配额，BYOK 只允许白名单域名，rerank 失败降级而非重试风暴。
7. **多轮记忆怎么做的？为什么先规则再改写？**
   历史只透传、不塞进向量检索；指代型追问先用关键词/长度规则判定（单轮 0/30 触发，省掉 100% 的无谓改写调用），命中才调 LLM 把"它"还原成"Session.send"，超时 8s 或失败回退原问题。追问召回 40% → 100%，代价是平均 3.73s/次、只在追问时付。
8. **改写组 100% 召回但 MRR 只有 0.858，差在哪？**
   8/10 排第一，两条改写没把符号名写进 query，掉到第 3/4 名。这是改写 prompt 的边界，不是检索链路的问题；和人工改写上限 1.000 的差距说明还有优化空间。

## 还需要补的（按优先级）

- [ ] 60 秒演示视频：提交仓库 → 进度 → 提问 → 流式回答 → 点引用定位
- [x] GitHub Actions 已配置并跑绿（ruff + pytest + 前端构建）
- [x] 多轮上下文评测：10 组追问 A/B/C 对比 + P0 token 成本（见 `docs/EVALUATION.md`）
- [ ] 把开发过程补成 issue → 分支 → PR 的闭环
- [ ] 域名 + HTTPS（备案通过后），BYOK 才能默认开启
