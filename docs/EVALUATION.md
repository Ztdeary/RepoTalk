# 检索消融评测

## 结论

在 30 题的 `requests` 仓库代码定位/用法数据集上：

- **纯向量检索基本不可用**：Recall@6 = 3.3%，30 题里只有 1 题召回正确文件。
- **混合检索（多路召回 + RRF + 符号/逐字精确匹配）是决定性提升**：Recall@6 = 95.0%，MRR = 0.967。
- **rerank 是精排补强，不是主力**：在 hybrid 基础上 Recall +3.3pp、MRR +1.7pp、nDCG +2.5pp，代价是单题检索耗时从 1.2s 涨到 2.2s（约 +0.98s/题）。

一句话：**这个项目的检索质量来自"混合召回 + 精确符号匹配"，不是来自向量模型；rerank 只值最后 3 个点，但要多付一倍延迟。**

## 实验设置

| 项 | 值 |
|---|---|
| 数据集 | `backend/tests/ragas_dataset.json`，30 题，10 题函数定位 / 10 题类定位 / 10 题用法 |
| 相关性口径 | 文件级二值：检索块的 `file_path` 属于 `ground_truth_contexts` 即相关 |
| 截断 | k = 6（与线上喂给 LLM 的块数 `RERANK_TOP_N` 一致） |
| 对比组 | `vector`（仅 pgvector top-6） / `hybrid`（多路召回+RRF，不重排） / `full`（hybrid+rerank） |
| 向量模型 | BAAI/bge-m3（1024 维，289 个代码块） |
| 重排模型 | BAAI/bge-reranker-v2-m3 |
| LLM 成本 | **0**（只做检索，不生成回答） |

## 结果

| 模式 | Recall@6 | Hit@6 | MRR@6 | nDCG@6 | 30 题耗时 |
|---|---|---|---|---|---|
| vector | 0.0333 | 0.0333 | 0.0167 | 0.0210 | 5.9s |
| hybrid | 0.9500 | 1.0000 | 0.9667 | 0.9415 | 33.7s |
| full | **0.9833** | **1.0000** | **0.9833** | **0.9666** | 63.3s |

### 分题型

| 模式 | 题型 | Recall@6 | MRR@6 | nDCG@6 |
|---|---|---|---|---|
| vector | func_location | 0.0000 | 0.0000 | 0.0000 |
| vector | class_location | 0.1000 | 0.0500 | 0.0631 |
| vector | usage | 0.0000 | 0.0000 | 0.0000 |
| hybrid | func_location | 1.0000 | 0.9500 | 0.9631 |
| hybrid | class_location | 1.0000 | 1.0000 | 1.0000 |
| hybrid | usage | 0.8500 | 0.9500 | 0.8613 |
| full | func_location | 1.0000 | 1.0000 | 1.0000 |
| full | class_location | 1.0000 | 0.9500 | 0.9631 |
| full | usage | 0.9500 | 1.0000 | 0.9368 |

## 怎么读这些数字

1. **vector 的 3.3% 不是脚本错误**，是真实结果：中文短 query 与英文大函数块在 bge-m3 空间里语义距离很远，top-6 大量落在 `utils.py`/`exceptions.py` 的通用代码上。这也解释了为什么线上必须依赖 BM25 与 symbol 精确召回。
2. **hybrid 已经把 top-6 里 5/6 的位置填对了**（MRR 0.967），剩下空间很小，所以 rerank 只能拿到 +3.3pp。
3. **rerank 的真实收益在顺序而非召回**：`func_location` 的 Recall 早就是 1.0，rerank 把 MRR 从 0.95 提到 1.0，等于"把定义块从第 2 名提到第 1 名"。对下游 LLM 的价值是减少上下文噪声，不是提高命中率。
4. **唯一明显的短板是 `usage` 题**：hybrid 的 Recall 仅 0.85，因为用法类问题的答案可能散落在 `docs/` 或 `sessions.py` 的 docstring 里，而当前检索只覆盖 AST 切分出的代码块。这是下一步最值得投入的地方（例如把 docstring/注释单独建索引）。

## 切分器跨平台修复后的复测（2026-11-07）

复跑消融时发现 `code_splitter._grammar_ready()` 在 Linux 上恒为 False（它只扫描 `tsl.cache_dir()` 里的 `.so/.dll`，而 Linux 的 grammar 随 wheel 安装、不在该目录）。后果是整个仓库静默降级成 `_simple_split`，切出的类块会缺类体。

同一份 `requests` 源码、同一个 Linux 容器内的前后对比：

| 指标 | 修复前（降级切分） | 修复后（AST 切分） |
|---|---|---|
| 代码块数 | 852 | **289** |
| Python 块语法不完整数 | 241（**28%**） | **0** |
| 切分耗时 | — | 0.1s |
| Recall@6（vector / hybrid / full） | 3.3% / 95.0% / 98.3% | **完全一致** |

两点结论：

1. 这个缺陷**不影响文件级召回**——降级块仍然落在正确的文件里，所以上面的消融数字没有变。它影响的是**交给 LLM 的上下文完整性**：28% 的 Python 块语法残缺，模型看到的是缺类体/缺缩进的片段。
2. 这恰好暴露了"文件级标注"口径的盲区：它能测出"找对文件没有"，测不出"块切得对不对"。要量化后半段，需要用 RAGAS（faithfulness / context_precision），而不是本轮指标。

修复后用修复逻辑重建了 `requests` collection（`pre_delete_collection=True` 整库覆盖），本文档数字均来自重建后的索引。

## 人工定位基线（代理测量）与检索延迟

被测仓库：`psf/requests`，**36 个 `.py` 文件 / 11,115 行**。

"人工定位基线"没法由脚本替人测（读代码、判断定义位置的时间无法模拟），所以这里测的是**可复现的代理指标**：模拟开发者拿到问题后最自然的一次检索（`func_location` 搜 `def <kw>`、`class_location` 搜 `class <kw>`、`usage` 搜 `<kw>`），统计"还要人工翻阅多少候选"。

| 指标 | 中位数 | 均值 |
|---|---|---|
| 候选文件数 | 1 | 4.6 |
| 候选命中行数 | 1 | 81.3 |

解读要小心：**中位数只有 1**，说明"已知符号名时，grep 本身就很快"——人工的真实成本在"不知道该搜什么"和"读上下文确认"。均值 4.6 文件 / 81.3 行被 `get`、`post` 这类通用词拉高。所以这组数字只能当**工作量下限**，不能当作"人工耗时"写进简历。

系统侧实测（`full` 模式，30 题，检索完成即推引用）：

| 指标 | 数值 |
|---|---|
| 检索 P50 | **1.792s** |
| 检索 P95 | **2.221s** |
| 检索均值 | 1.799s |

> 该延迟**不含 LLM 生成**；SSE 在检索完成后立即下发引用，所以"首个引用出现"≈ 检索耗时。

**真实人工耗时怎么补（10 分钟，必须自己测）**：挑 10 道同类型题，只靠 `grep`/IDE 找定义并确认，用秒表逐题计时，记平均值 X。之后简历写成"单次定位由人工平均 **X 秒** 降至 **1.8 秒（P50）**"。没有 X 就不要写时间对比，只写 P50 + 召回率，避免面试被追问时无法自证。

明细数据：[week3_baseline.json](../backend/tests/week3_baseline.json)

## 多轮上下文评测（追加，2026-11-07）

单轮评测测不出"追问"——用户说"它默认的超时是多少"，字面上没有任何可检索的符号。这一组专门测**上下文记忆**：10 组"首轮问题 + 纯指代追问"，同一个 `requests` collection、同一 k=6、同一 `full` 检索模式，只改检索 query。

| 组 | 检索 query | Recall@6 | Hit@6 | MRR@6 | nDCG@6 |
|---|---|---|---|---|---|
| A | 追问原句（不改写） | 0.4000 | 0.4000 | 0.1833 | 0.2361 |
| B | 人工还原成独立问题（上限） | **1.0000** | **1.0000** | **1.0000** | **1.0000** |
| C | LLM 改写（实际实现） | **1.0000** | **1.0000** | 0.8583 | 0.8931 |

- **A→C**：Recall@6 从 **40% 到 100%**，MRR 从 **0.183 到 0.858**。不改写时 10 题里有 6 题 top-6 完全找不到正确文件。
- **C→B**：差距在排序而非召回。C 组 8/10 排第 1；Q6、Q8 的改写没把符号名带进 query，掉到第 4 / 第 3 名。
- **触发率**：指代型追问 **10/10** 触发改写；30 条单轮问题 **0/30** 触发（首轮零额外 LLM 调用、零额外成本）。
- **改写成本**：平均 **3.73s/次**，只在追问时付；超时 8s 或失败直接回退原问题。
- **P0 历史注入成本**（同一问题带/不带历史各答一次，读模型返回的 `usage`）：prompt_tokens 增量 **+7 / +10 / +8**（9840→9847、9377→9387、7123→7131），即历史块本身只有 24–34 字符，相比 7k–9k 的上下文可忽略。
- **延迟不能拿来说事**：带历史后 2.62→6.45s、12.6→9.72s、9.58→15.44s，方向不一致，3 样本 + 公网 LLM 抖动，**不构成"变快/变慢"的证据**。

> 口径说明：本轮 LLM 走 SiliconFlow 的 `deepseek-ai/DeepSeek-V3`（当日 DeepSeek 官方接口返回 500，非本项目问题）。改写质量与模型相关，换模型需重测；检索指标本身与生成模型无关。

明细：[week7_memory_eval.json](../backend/tests/week7_memory_eval.json) / [week7_memory_eval.md](../backend/tests/week7_memory_eval.md)

复现（`backend/` 目录下，需先 ingest 并存在 `tests/.cache/requests`）：

```bash
.venv/Scripts/python.exe -m tests.eval_memory
# DeepSeek 官方接口不可用时切 SiliconFlow
set W7_FORCE_LLM=siliconflow && .venv/Scripts/python.exe -m tests.eval_memory
```

## 口径与局限（不要过度解读）

- **文件级标注偏粗**：同一文件里的所有块都算"相关"，因此召回容易偏高、precision 类指标会失真。这里只看 Recall/MRR/nDCG，不报 Precision。它同样测不出"块切得对不对"——上面 28% → 0 的块完整性问题，只能靠 RAGAS 或人工抽检暴露。
- **30 题、单仓库**：统计噪声不小，且只代表 `requests` 这种结构清晰的 Python 库；对大型多语言仓库结论可能不同。
- **只衡量检索，不衡量回答**：检索对了不代表 LLM 答对。端到端质量（faithfulness / answer_relevancy）需要 RAGAS，另行跑，不在本表内。
- **多轮组只有 10 组、10 条追问**：A 组 40% 已经说明问题，但 10 条样本的 MRR 波动大；B 组是人工写的"理想改写"，只能当上限参考，不代表生产里一定拿得到。
- **vector 组的 k=6 与线上 `answer()` 的 k=4 不同**：为了三组同口径比较统一取 6；结论方向不受影响。
- 指标实现见 `backend/tests/eval/metrics.py`；所有指标先按文件去重，避免同一文件的多个块把分数刷高。

## 复现

```bash
cd backend
# 三组全跑
.venv/Scripts/python.exe -m tests.test_week3
# 冒烟（前 3 题）
.venv/Scripts/python.exe -m tests.test_week3 --limit 3
# 只跑某几组
.venv/Scripts/python.exe -m tests.test_week3 --modes vector,hybrid
```

前置：`docker compose up -d`（db + redis）、`requests` collection 已 ingest、`tests/.cache/requests` 源码存在。

产物：

- `backend/tests/week3_ablation.json`：每组指标 + 每题召回的文件路径（可复盘）
- `backend/tests/week3_ablation.md`：自动生成的表格

## 改动说明

- 新增 `backend/tests/eval/metrics.py`：Recall@k / Hit@k / MRR@k / nDCG@k，纯函数、无第三方依赖
- 新增 `backend/tests/test_eval_metrics.py`：7 个纯单测（不连库、不调模型）
- `backend/app/rag/hybrid_retriever.py`：`hybrid_search` 新增 `mode` 参数（`vector` / `hybrid` / `full`），**默认 `full`，线上行为不变**
- `backend/tests/test_week3.py`：从"单组对比脚本"改为参数化消融实验，去掉硬编码路径，输出 JSON + Markdown
