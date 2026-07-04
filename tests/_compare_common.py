"""对比测试公共逻辑。"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
from cipx import CIPX
from cipx.config import Settings as CSettings

from isbnx import ISBNX
from isbnx.config import Settings as ISettings

Engine = CIPX | ISBNX

# ── strict 等级映射（统一为"ISBN 有效即成功"） ──
# cipx: 6 = ISBN valid, isbnx: 2 = ISBN valid
CIPX_STRICT = 6
ISBNX_STRICT = 2


def run_one(engine: Engine, method: str, path: Path) -> dict:
    """用单个引擎处理一个文件，返回耗时和结果。"""
    t0 = time.perf_counter()
    try:
        result = getattr(engine, method)(path)
        elapsed = time.perf_counter() - t0
        return {
            "isbn": result.bookinfo.isbn or "",
            "valid": result.success,
            "elapsed": elapsed,
            "error": result.error or "",
        }
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return {"isbn": "", "valid": False, "elapsed": elapsed, "error": str(e)}


def run_benchmark(
    *,
    data_dir: Path,
    output_csv: Path,
    title: str,
    suffix: str,
    method: str,
    suffixes: set[str] | None = None,
) -> None:
    """完整的对比测试流程。

    Args:
        data_dir: 样本目录。
        output_csv: 输出 CSV 路径。
        title: 打印的标题。
        suffix: 单后缀时直接指定。
        method: 调用的引擎方法名 (如 ``from_image``)。
        suffixes: 多后缀集合。
    """
    allowed = {suffix} if suffixes is None else suffixes

    files = [f for f in sorted(data_dir.iterdir()) if f.suffix.lower() in allowed]
    print(f"  找到 {len(files)} 个文件")

    print("\n预热引擎...")
    t0 = time.perf_counter()
    cipx = CIPX(config=CSettings(strict=CIPX_STRICT))
    print(f"  cipx 预热完成 (strict={CIPX_STRICT}): {time.perf_counter() - t0:.1f}s")
    t0 = time.perf_counter()
    isbnx = ISBNX(config=ISettings(strict=ISBNX_STRICT))
    print(f"  isbnx 预热完成 (strict={ISBNX_STRICT}): {time.perf_counter() - t0:.1f}s")

    print(f"\n开始测试 {len(files)} 个文件...")
    rows: list[dict] = []
    t_start = time.perf_counter()

    for i, path in enumerate(files, 1):
        if i % 30 == 0 or i == 1:
            print(f"  [{i}/{len(files)}] {path.name}")

        row = {"file": path.name}

        cipx_res = run_one(cipx, method, path)
        row["cipx_isbn"] = cipx_res["isbn"]
        row["cipx_valid"] = cipx_res["valid"]
        row["cipx_elapsed"] = round(cipx_res["elapsed"], 4)
        row["cipx_error"] = cipx_res["error"]

        isbnx_res = run_one(isbnx, method, path)
        row["isbnx_isbn"] = isbnx_res["isbn"]
        row["isbnx_valid"] = isbnx_res["valid"]
        row["isbnx_elapsed"] = round(isbnx_res["elapsed"], 4)
        row["isbnx_error"] = isbnx_res["error"]

        both_valid = cipx_res["valid"] and isbnx_res["valid"]
        both_invalid = not cipx_res["valid"] and not isbnx_res["valid"]
        if both_valid:
            row["match"] = str(cipx_res["isbn"] == isbnx_res["isbn"])
        elif both_invalid:
            row["match"] = "True"
        else:
            row["match"] = "False"

        rows.append(row)

    total_elapsed = time.perf_counter() - t_start
    print(f"\n测试完成，耗时 {total_elapsed:.1f}s")

    df = pd.DataFrame(rows)
    n = len(df)
    cipx_ok = df["cipx_valid"].sum()
    isbnx_ok = df["isbnx_valid"].sum()
    both_ok = ((df["cipx_valid"]) & (df["isbnx_valid"])).sum()
    match_count = (df["match"] == "True").sum()
    only_cipx = ((df["cipx_valid"]) & (~df["isbnx_valid"])).sum()
    only_isbnx = ((~df["cipx_valid"]) & (df["isbnx_valid"])).sum()

    summary = pd.DataFrame(
        [
            ["样本总数", n],
            ["cipx 成功数", cipx_ok],
            ["cipx 成功率", f"{cipx_ok / n * 100:.1f}%"],
            ["isbnx 成功数", isbnx_ok],
            ["isbnx 成功率", f"{isbnx_ok / n * 100:.1f}%"],
            ["两者都成功", both_ok],
            ["仅 cipx 成功", only_cipx],
            ["仅 isbnx 成功", only_isbnx],
            ["结果一致数", match_count],
            ["一致率", f"{match_count / n * 100:.1f}%"],
            ["cipx 总耗时", f"{df['cipx_elapsed'].sum():.1f}s"],
            ["cipx 平均耗时", f"{df['cipx_elapsed'].mean() * 1000:.1f}ms"],
            ["isbnx 总耗时", f"{df['isbnx_elapsed'].sum():.1f}s"],
            ["isbnx 平均耗时", f"{df['isbnx_elapsed'].mean() * 1000:.1f}ms"],
            ["cipx 中位数耗时", f"{df['cipx_elapsed'].median() * 1000:.1f}ms"],
            ["isbnx 中位数耗时", f"{df['isbnx_elapsed'].median() * 1000:.1f}ms"],
        ],
        columns=["指标", "值"],
    )

    # ── 不一致明细 ──
    mismatches = df[df["match"] == "False"]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", encoding="utf-8-sig") as f:
        # ① 逐行数据
        df.to_csv(f, index=False, lineterminator="\n")
        # ② 汇总
        f.write("\n")
        summary.to_csv(f, index=False, lineterminator="\n")
        # ③ 不一致明细
        if len(mismatches) > 0:
            f.write("\n\n")
            f.write("不一致明细\n")
            mismatches.to_csv(f, index=False, lineterminator="\n")

    print(f"\n结果已保存至: {output_csv}")
    print("\n=== 汇总 ===")
    for _, r in summary.iterrows():
        print(f"  {r['指标']}: {r['值']}")

    if len(mismatches) > 0:
        print(f"\n=== 不一致样本 ({len(mismatches)} 条) ===")
        for _, r in mismatches.iterrows():
            print(f"  {r['file']}")
            print(f"    cipx:  isbn={r['cipx_isbn']!r}  valid={r['cipx_valid']}  {r['cipx_error']}")
            print(f"    isbnx: isbn={r['isbnx_isbn']!r}  valid={r['isbnx_valid']}  {r['isbnx_error']}")
