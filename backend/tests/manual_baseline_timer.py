"""人工定位基线计时器:逐题记录"人自己找答案"要花多少秒。

为什么需要它:
    检索指标只能证明"系统找得准",证明不了"比人快"。人工耗时无法由脚本模拟,
    只能用秒表实测。这个脚本负责计时、留痕、算统计,避免手记时间后无法自证。

评测规则(必须遵守,否则数据不可用):
    1. 全程只用 grep / IDE 全局搜索 / 文件树,不许问本系统,不许问 AI。
    2. 计时从"看到题目"开始,到"确认找到定义所在文件"结束;读代码理解不算。
    3. 同一仓库(默认 tests/.cache/requests),先热身 1 题(不计入),再跑正式 10 题。
    4. 题目按题型轮转出现,避免连续同类题产生学习效应。

用法(backend/ 目录下):
    .venv/Scripts/python.exe -m tests.manual_baseline_timer
    .venv/Scripts/python.exe -m tests.manual_baseline_timer --dry-run   # 只看题目
    .venv/Scripts/python.exe -m tests.manual_baseline_timer --fresh     # 清空重测
    .venv/Scripts/python.exe -m tests.manual_baseline_timer --ids 4,5,6,11,13,21,24,29

产物:
    tests/manual_baseline.json   逐题记录(可复盘,中断后可续测)
    tests/manual_baseline.md     汇总表格 + 可直接抄进简历的句子
"""

import argparse
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
DEFAULT_DATASET = HERE / "ragas_dataset.json"
OUT_JSON = HERE / "manual_baseline.json"
OUT_MD = HERE / "manual_baseline.md"

# 默认题型配额:与线上 30 题数据集 10/10/10 的比例一致,取 10 题
DEFAULT_PER_TYPE = "4,3,3"
TYPE_ORDER = ("func_location", "class_location", "usage")
TYPE_LABELS = {"func_location": "函数定位", "class_location": "类定位", "usage": "用法"}


def normalize_path(path: str) -> str:
    """把用户输入/标注路径归一化到 src/requests/xxx.py 这种形式。"""
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def pick_questions(dataset: list[dict], per_type: str) -> list[dict]:
    """按题型各取 N 题,再轮转交错,避免同类型连续出现。"""
    quotas = [int(x) for x in per_type.split(",")]
    if len(quotas) != len(TYPE_ORDER):
        raise SystemExit(f"--per-type 需要 {len(TYPE_ORDER)} 个数字,例如 {DEFAULT_PER_TYPE}")
    queues, picked = [], []
    for qtype, quota in zip(TYPE_ORDER, quotas):
        items = [x for x in dataset if x.get("type") == qtype and x.get("ground_truth_contexts")]
        queues.append(items[:quota])
        picked.extend(items[:quota])
    merged = []
    while any(queues):
        for queue in queues:
            if queue:
                merged.append(queue.pop(0))
    merged_ids = {x["id"] for x in merged}
    if len(merged) != len(picked) or len(merged_ids) != len(picked):
        raise SystemExit("题目选择异常:按题型取到的题不足或重复")
    return merged


def measure_one(item: dict, index: int, total: int) -> dict:
    """对一道题计时并记录用户定位到的文件。"""
    print(f"\n[{index}/{total}] 题型:{TYPE_LABELS.get(item['type'], item['type'])}")
    print(f"题目:{item['question']}")
    print("提示:只用 grep / IDE 搜索;找到定义所在文件就停,不要读完整段代码。")

    while True:
        try:
            input("\n按 Enter 开始计时…")
            started = time.perf_counter()
            input("计时中…找到并确认后按 Enter 停止。")
            seconds = round(time.perf_counter() - started, 2)
        except EOFError:
            raise
        print(f"→ 本题用时 {seconds}s")

        found = input("你定位到的文件(含路径或文件名,直接回车=没找到):")
        found_norm = normalize_path(found)
        expected = [normalize_path(p) for p in item["ground_truth_contexts"]]
        if not found_norm:
            correct = False
        else:
            correct = any(
                found_norm == e or found_norm.endswith("/" + e) or e.endswith("/" + found_norm)
                for e in expected
            )
        print("判定:" + ("命中" if correct else "未命中") + f"  参考文件={', '.join(expected)}")

        action = input("[Enter] 记下本题  [r] 重测本题  [s] 跳过本题:").strip().lower()
        if action == "r":
            print("重测本题,上一条记录作废。")
            continue
        if action == "s":
            return {"id": item["id"], "type": item["type"], "question": item["question"],
                    "seconds": None, "found_file": None, "correct": False, "skipped": True,
                    "measured_at": datetime.now().isoformat(timespec="seconds")}
        return {"id": item["id"], "type": item["type"], "question": item["question"],
                "seconds": seconds, "found_file": found_norm or None, "correct": correct, "skipped": False,
                "measured_at": datetime.now().isoformat(timespec="seconds")}


def compute_stats(records: list[dict]) -> dict:
    """只统计真正计时且命中的题;未命中单独计数,不混进平均值。"""
    done = [r for r in records if not r.get("skipped") and r.get("seconds") is not None]
    correct = [r for r in done if r.get("correct")]
    stats = {
        "n_total": len(records),
        "n_measured": len(done),
        "n_correct": len(correct),
        "n_miss": len(done) - len(correct),
    }
    if correct:
        seconds = [r["seconds"] for r in correct]
        stats.update({
            "mean_sec": round(statistics.fmean(seconds), 2),
            "median_sec": round(statistics.median(seconds), 2),
            "min_sec": round(min(seconds), 2),
            "max_sec": round(max(seconds), 2),
        })
    if done:
        stats["miss_rate"] = round((len(done) - len(correct)) / len(done), 4)
    by_type = {}
    for qtype in TYPE_ORDER:
        seconds = [r["seconds"] for r in correct if r["type"] == qtype]
        if seconds:
            by_type[qtype] = {"n": len(seconds), "mean_sec": round(statistics.fmean(seconds), 2)}
    stats["by_type"] = by_type
    return stats


def render_markdown(records: list[dict], stats: dict) -> str:
    """渲染人可直接读的汇总,并给出简历句式。"""
    lines = [
        "# 人工定位基线(实测)",
        "",
        f"- 时间:{datetime.now().isoformat(timespec='seconds')}",
        f"- 题数:{stats['n_measured']} 题(命中 {stats['n_correct']} / 未命中 {stats['n_miss']})",
        "- 规则:只用 grep / IDE,不询问本系统,计时到\"确认文件\"为止",
        "",
        "| # | 题型 | 题目 | 用时(s) | 定位文件 | 命中 |",
        "|---|---|---|---|---|---|",
    ]
    for r in records:
        if r.get("skipped"):
            continue
        lines.append(
            f"| {r['id']} | {TYPE_LABELS.get(r['type'], r['type'])} | {r['question']} | "
            f"{r['seconds']} | {r.get('found_file') or '-'} | {'是' if r.get('correct') else '否'} |"
        )
    lines += ["", "## 统计", ""]
    if "mean_sec" in stats:
        lines.append(
            f"- 命中题平均 **{stats['mean_sec']}s**,中位数 {stats['median_sec']}s,"
            f"区间 {stats['min_sec']}–{stats['max_sec']}s"
        )
        lines.append(f"- 未命中率 {stats.get('miss_rate', 0) * 100:.1f}%")
        for qtype, row in stats["by_type"].items():
            lines.append(f"- {TYPE_LABELS.get(qtype, qtype)}:{row['n']} 题,平均 {row['mean_sec']}s")
        if stats["mean_sec"] > 1.8:
            lines += [
                "",
                "## 可写进简历的句子",
                "",
                f"单次代码定位由人工平均 **{stats['mean_sec']} 秒**降至 **1.8 秒(P50)**,"
                f"即检索链路把定位耗时压缩 **{(1 - 1.8 / stats['mean_sec']) * 100:.0f}%**。",
            ]
        else:
            lines += [
                "",
                "## 提示",
                "",
                f"人工平均 {stats['mean_sec']}s 不高于系统 P50 1.8s,说明这批题太简单,"
                "不足以支撑\"提效\"结论;换更难的题再测,或只写召回指标、不写时间对比。",
            ]
    else:
        lines.append("- 还没有命中题的有效计时,数据不足。")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="人工定位基线计时器")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--per-type", default=DEFAULT_PER_TYPE, help="三类题各取几题,默认 4,3,3")
    parser.add_argument("--ids", default="", help="直接指定题目 id,逗号分隔(覆盖 --per-type)")
    parser.add_argument("--out", default=str(OUT_JSON))
    parser.add_argument("--md-out", default=str(OUT_MD))
    parser.add_argument("--fresh", action="store_true", help="忽略历史记录,从头测")
    parser.add_argument("--dry-run", action="store_true", help="只打印题目,不计时")
    args = parser.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    if args.ids:
        wanted = {int(x) for x in args.ids.split(",") if x.strip()}
        questions = [x for x in dataset if x["id"] in wanted]
        missing = wanted - {x["id"] for x in questions}
        if missing:
            raise SystemExit(f"数据集里没有这些 id:{sorted(missing)}")
    else:
        questions = pick_questions(dataset, args.per_type)

    if args.dry_run:
        print(f"共 {len(questions)} 题:")
        for i, q in enumerate(questions, 1):
            print(f"  [{i}] {TYPE_LABELS.get(q['type'], q['type'])}  {q['question']}")
        return

    out = Path(args.out)
    records = []
    if out.exists() and not args.fresh:
        try:
            records = json.loads(out.read_text(encoding="utf-8")).get("records", [])
        except (json.JSONDecodeError, AttributeError):
            records = []
    done_ids = {r["id"] for r in records if not r.get("skipped")}
    questions = [q for q in questions if q["id"] not in done_ids]
    if done_ids:
        print(f"续测:跳过已完成 {len(done_ids)} 题(--fresh 可重新开始)")

    print("=" * 64)
    print("人工定位基线实测")
    print("规则:只用 grep / IDE,不询问本系统;点引用确认后立即停止计时")
    print("先热身 1 题(不计入),再开始正式计时;Ctrl+C 可随时中断,进度自动保存")
    print("=" * 64)

    try:
        for index, item in enumerate(questions, 1):
            record = measure_one(item, index, len(questions))
            records.append(record)
            stats = compute_stats(records)
            out.write_text(
                json.dumps({"meta": {"dataset": Path(args.dataset).name, "repo": "psf/requests",
                                     "k": 6, "bm25_chunks": 261}, "stats": stats, "records": records},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"已保存({stats['n_measured']} 题已计时)。")
    except (KeyboardInterrupt, EOFError):
        print("\n已中断,保留当前进度。")

    records_all = records
    stats = compute_stats(records_all)
    out.write_text(
        json.dumps({"meta": {"dataset": Path(args.dataset).name, "repo": "psf/requests",
                             "k": 6, "bm25_chunks": 261}, "stats": stats, "records": records_all},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown = render_markdown(records_all, stats)
    Path(args.md_out).write_text(markdown, encoding="utf-8")
    print("\n" + markdown)
    print(f"明细:{args.out}\n表格:{args.md_out}")


if __name__ == "__main__":
    main()
