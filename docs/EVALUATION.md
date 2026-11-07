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

## 口径与局限（不要过度解读）

- **文件级标注偏粗**：同一文件里的所有块都算"相关"，因此召回容易偏高、precision 类指标会失真。这里只看 Recall/MRR/nDCG，不报 Precision。它同样测不出"块切得对不对"——上面 28% → 0 的块完整性问题，只能靠 RAGAS 或人工抽检暴露。
- **30 题、单仓库**：统计噪声不小，且只代表 `requests` 这种结构清晰的 Python 库；对大型多语言仓库结论可能不同。
- **只衡量检索，不衡量回答**：检索对了不代表 LLM 答对。端到端质量（faithfulness / answer_relevancy）需要 RAGAS，另行跑，不在本表内。
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
