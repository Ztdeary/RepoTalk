"""多轮上下文记忆评测:追问召回 A/B/C 对比 + P0 提示词成本。

实验设计(全部在同一个 collection、同一 k=6、同为 full 检索模式):

  A 组:直接用追问原句检索("它默认的超时是多少?")
  B 组:人工把追问还原成独立问题检索(这是改写效果的上限)
  C 组:用 rewrite_query() 真实改写后检索(受控于开关,默认关闭)

指标:文件级 Recall@6 / MRR@6 / Hit@6。
A→C 的差值就是"查询改写"带来的真实收益; C 与 B 的差距说明改写质量。

P0(历史注入)不改变检索,所以单独测它的成本:
  - prompt_tokens 增量(直接读模型返回的 usage)
  - 生成耗时增量
  - 同时把两版回答打出来,便于人工判断是否"接得上上文"

大模型:默认走 .env 里的 DeepSeek;若它 500,脚本会自动切到
SiliconFlow(复用 EMBEDDING_API_KEY),并把实际使用的 provider 写进结果。

用法(backend/ 目录下):
    .venv/Scripts/python.exe -m tests.eval_memory
"""

import asyncio
import json
import os
import time
from pathlib import Path

HERE = Path(__file__).parent
BACKEND = HERE.parent
ENV_FILE = BACKEND / ".env"
OUT_JSON = HERE / "week7_memory_eval.json"
OUT_MD = HERE / "week7_memory_eval.md"


def _read_env(key: str, default: str = "") -> str:
    """从 backend/.env 读一个变量,避免脚本依赖 python-dotenv。"""
    if not ENV_FILE.exists():
        return default
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


# 必须在 import app.* 之前改环境变量:pydantic Settings 的优先级是
# 环境变量 > .env 文件,这样既能复用真实配置,又不会把 .env 改坏。
if os.environ.get("W7_FORCE_LLM") == "siliconflow":
    os.environ["LLM_BASE_URL"] = "https://api.siliconflow.cn/v1"
    os.environ["LLM_MODEL"] = "deepseek-ai/DeepSeek-V3"
    os.environ["LLM_API_KEY"] = _read_env("EMBEDDING_API_KEY")

from app.core.config import settings  # noqa: E402
from app.rag.hybrid_retriever import hybrid_search  # noqa: E402
from app.rag.pipeline import generate_answer, rewrite_query  # noqa: E402
from app.rag.memory import build_history_block, should_rewrite  # noqa: E402
from tests.test_week3 import build_resources, run_mode  # noqa: E402

# 10 组追问:history 只给首轮问题,追问本身不含标识符(全靠上文补全)
SESSIONS = [
    {
        "id": 1,
        "history": [("user", "`Session.send` 在哪定义?")],
        "followup": "它默认的超时时间是多少?",
        "oracle": "Session.send 的默认超时时间是多少?",
        "gold": ["src/requests/sessions.py"],
    },
    {
        "id": 2,
        "history": [("user", "`PreparedRequest.prepare` 做什么?")],
        "followup": "它内部调用了哪些方法?",
        "oracle": "PreparedRequest.prepare 内部调用了哪些方法?",
        "gold": ["src/requests/models.py"],
    },
    {
        "id": 3,
        "history": [("user", "`HTTPAdapter.send` 在哪定义?")],
        "followup": "它的最大重试次数默认是多少?",
        "oracle": "HTTPAdapter.send 的最大重试次数默认是多少?",
        "gold": ["src/requests/adapters.py"],
    },
    {
        "id": 4,
        "history": [("user", "`dispatch_hook` 在哪定义?")],
        "followup": "它默认注册了哪些钩子?",
        "oracle": "dispatch_hook 默认注册了哪些钩子?",
        "gold": ["src/requests/hooks.py"],
    },
    {
        "id": 5,
        "history": [("user", "`Session.resolve_redirects` 做什么?")],
        "followup": "它最多跟随多少次重定向?",
        "oracle": "Session.resolve_redirects 最多跟随多少次重定向?",
        "gold": ["src/requests/sessions.py"],
    },
    {
        "id": 6,
        "history": [("user", "`Response.raise_for_status` 做什么?")],
        "followup": "它抛出的是哪种异常?",
        "oracle": "Response.raise_for_status 抛出的是哪种异常?",
        "gold": ["src/requests/models.py"],
    },
    {
        "id": 7,
        "history": [("user", "`Request.prepare_url` 做什么?")],
        "followup": "它怎么处理非 ASCII 参数?",
        "oracle": "Request.prepare_url 怎么处理非 ASCII 参数?",
        "gold": ["src/requests/models.py"],
    },
    {
        "id": 8,
        "history": [("user", "`requests.utils.super_len` 做什么?")],
        "followup": "它对文件对象怎么处理?",
        "oracle": "requests.utils.super_len 对文件对象怎么处理?",
        "gold": ["src/requests/utils.py"],
    },
    {
        "id": 9,
        "history": [("user", "`Session.merge_environment_settings` 做什么?")],
        "followup": "它合并了哪些来源的设置?",
        "oracle": "Session.merge_environment_settings 合并了哪些来源的设置?",
        "gold": ["src/requests/sessions.py"],
    },
    {
        "id": 10,
        "history": [("user", "`RequestEncodingMixin._encode_params` 做什么?")],
        "followup": "它支持哪些数据类型?",
        "oracle": "RequestEncodingMixin._encode_params 支持哪些数据类型?",
        "gold": ["src/requests/models.py"],
    },
]


def _dataset(questions: list[str]) -> list[dict]:
    return [
        {
            "id": s["id"],
            "type": "multi_turn",
            "question": q,
            "ground_truth_contexts": s["gold"],
        }
        for s, q in zip(SESSIONS, questions)
    ]


def main():
    if os.environ.get("W7_FORCE_LLM") == "siliconflow":
        # 显式指到 SiliconFlow 时,这里仍可能读到 .env 里的 DeepSeek Key
        os.environ["LLM_API_KEY"] = _read_env("EMBEDDING_API_KEY")

    repo_root = HERE / ".cache" / "requests"
    if not repo_root.is_dir():
        raise SystemExit(f"源码目录不存在:{repo_root}")
    store, bm25, n_chunks = build_resources("requests", repo_root)
    print(f"[0] BM25 块数={n_chunks}  LLM={settings.llm_model} @ {settings.llm_base_url}")

    # ---------- 1. 改写:先真实跑一遍,得到 C 组查询 ----------
    print("[1] 查询改写(10 次短调用)")
    rewritten, rewrite_elapsed = [], 0.0
    for s in SESSIONS:
        started = time.perf_counter()
        query = None
        try:
            query = asyncio.run(
                rewrite_query(s["followup"], [{"role": r, "content": c} for r, c in s["history"]])
            )
        except Exception as exc:
            print(f"  Q{s['id']} rewrite ERR={type(exc).__name__}: {exc}")
        rewrite_elapsed += time.perf_counter() - started
        rewritten.append(query or s["followup"])
        print(f"  Q{s['id']:>2} 原句={s['followup']} -> 改写={rewritten[-1]}")
    print(f"      改写总耗时={rewrite_elapsed:.1f}s 平均={rewrite_elapsed / len(SESSIONS):.2f}s")

    # ---------- 2. A/B/C 三组检索 ----------
    print("[2] A/B/C 检索对比")
    datasets = {
        "A_raw": _dataset([s["followup"] for s in SESSIONS]),
        "B_oracle": _dataset([s["oracle"] for s in SESSIONS]),
        "C_rewrite": _dataset(rewritten),
    }
    results = {}
    for name, dataset in datasets.items():
        summary, elapsed = run_mode("full", dataset, store, bm25, k=6)
        summary["elapsed_sec"] = elapsed
        results[name] = summary
        overall = summary["overall"]
        print(
            f"  {name}: Recall={overall['recall']:.4f} Hit={overall['hit']:.4f} "
            f"MRR={overall['mrr']:.4f} nDCG={overall['ndcg']:.4f} ({elapsed:.1f}s)"
        )

    # ---------- 3. 触发率:10 条追问 vs 30 条单轮 --------------
    follow_up_hits = sum(1 for s in SESSIONS if should_rewrite(s["followup"], [{"role": r, "content": c} for r, c in s["history"]]))
    single = json.loads((HERE / "ragas_dataset.json").read_text(encoding="utf-8"))
    single_hits = sum(1 for item in single if should_rewrite(item["question"], []))
    trigger = {
        "multi_turn_triggered": f"{follow_up_hits}/{len(SESSIONS)}",
        "single_turn_triggered": f"{single_hits}/{len(single)}",
    }
    print(f"[3] 改写触发率:追问 {trigger['multi_turn_triggered']} / 单轮 {trigger['single_turn_triggered']}")

    # ---------- 4. P0:历史注入的 token / 延迟成本 ----
    print("[4] P0 历史注入成本(同一问题,带/不带历史各答一次)")
    p0 = []
    for s in SESSIONS[:3]:
        docs = hybrid_search(s["followup"], store, bm25, mode="full")
        history = [{"role": r, "content": c} for r, c in s["history"]]
        record = {"id": s["id"], "question": s["followup"]}
        for label, hist in (("without_history", None), ("with_history", history)):
            started = time.perf_counter()
            result = generate_answer(s["followup"], docs, None, 320, hist)
            record[label] = {
                "elapsed_sec": round(time.perf_counter() - started, 2),
                "prompt_tokens": (result.get("usage") or {}).get("prompt_tokens"),
                "completion_tokens": (result.get("usage") or {}).get("completion_tokens"),
                "answer_chars": len(result.get("answer") or ""),
                "answer": result.get("answer"),
            }
        delta = None
        if record["with_history"]["prompt_tokens"] and record["without_history"]["prompt_tokens"]:
            delta = record["with_history"]["prompt_tokens"] - record["without_history"]["prompt_tokens"]
        record["prompt_token_delta"] = delta
        record["history_block_chars"] = len(build_history_block(history))
        p0.append(record)
        print(
            f"  Q{s['id']}: prompt_tokens {record['without_history']['prompt_tokens']} -> "
            f"{record['with_history']['prompt_tokens']} (+{delta}); "
            f"latency {record['without_history']['elapsed_sec']}s -> {record['with_history']['elapsed_sec']}s"
        )

    report = {
        "meta": {
            "collection": "requests",
            "repo": "psf/requests",
            "bm25_chunks": n_chunks,
            "k": 6,
            "mode": "full",
            "n_multi_turn": len(SESSIONS),
            "llm_model": settings.llm_model,
            "llm_base_url": settings.llm_base_url,
            "rewrite_avg_sec": round(rewrite_elapsed / len(SESSIONS), 2),
        },
        "retrieval": {
            name: {
                "overall": summary["overall"],
                "elapsed_sec": round(summary["elapsed_sec"], 1),
                "per_query": [
                    {"id": q["id"], "first_rank": q["first_rank"]} for q in summary["per_query"]
                ],
            }
            for name, summary in results.items()
        },
        "trigger": trigger,
        "rewrites": [{"id": s["id"], "raw": s["followup"], "rewritten": r} for s, r in zip(SESSIONS, rewritten)],
        "p0": p0,
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    a, b, c = results["A_raw"]["overall"], results["B_oracle"]["overall"], results["C_rewrite"]["overall"]
    md = f"""# 多轮上下文评测(collection=requests, k=6, 模式=full)

- 数据:psf/requests,{len(SESSIONS)} 组"首轮 + 指代型追问"
- 模型:{settings.llm_model}({settings.llm_base_url})
- 改写平均耗时:{report['meta']['rewrite_avg_sec']}s/次

| 组 | 检索 query | Recall@6 | Hit@6 | MRR@6 | nDCG@6 |
|---|---|---|---|---|---|
| A | 追问原句(不改写) | {a['recall']:.4f} | {a['hit']:.4f} | {a['mrr']:.4f} | {a['ndcg']:.4f} |
| B | 人工还原独立问题(上限) | {b['recall']:.4f} | {b['hit']:.4f} | {b['mrr']:.4f} | {b['ndcg']:.4f} |
| C | LLM 改写(实际实现) | {c['recall']:.4f} | {c['hit']:.4f} | {c['mrr']:.4f} | {c['ndcg']:.4f} |

## 改写触发率

- 追问触发: {trigger['multi_turn_triggered']}
- 单轮触发: {trigger['single_turn_triggered']}(首轮零额外 LLM 调用)

## P0 历史注入成本(3 组)

| 题目 | prompt_tokens 无历史 | 有历史 | 增量 | 生成耗时 无→有 |
|---|---|---|---|---|
"""
    for row in p0:
        md += (
            f"| Q{row['id']} | {row['without_history']['prompt_tokens']} | "
            f"{row['with_history']['prompt_tokens']} | +{row['prompt_token_delta']} | "
            f"{row['without_history']['elapsed_sec']}s → {row['with_history']['elapsed_sec']}s |\n"
        )
    OUT_MD.write_text(md, encoding="utf-8")
    print("[5] 写入", OUT_JSON, "与", OUT_MD)
    print(md)


if __name__ == "__main__":
    main()
