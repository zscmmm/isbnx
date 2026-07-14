"""批量处理真实运行演示脚本。

从 tests/data 选取少量代表性文件，复制到临时目录后执行批量处理，
打印各参数的效果对比，方便观察 entries_callback 的行为。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from isbnx.batch import Batch, BatchConfig, BatchResult

DATA_DIR = Path(__file__).parent / "tests" / "data"


# ── 辅助 ──


def _pick_samples() -> list[Path]:
    """从各子目录选最小的 2 个文件。"""
    files: list[Path] = []
    for subdir in ("epubs", "pdfs", "mobi", "images", "7z", "archives"):
        d = DATA_DIR / subdir
        if not d.exists():
            continue
        candidates = sorted(
            (f for f in d.iterdir() if f.is_file()),
            key=lambda f: f.stat().st_size,
        )
        files.extend(candidates[:2])
    # 额外补一个文件名含 ISBN 的 epub
    isbn_epubs = sorted((DATA_DIR / "epubs").glob("*978*"))
    if isbn_epubs:
        files.append(isbn_epubs[0])
    # 去重
    seen = set()
    return [f for f in files if f.name not in seen and not seen.add(f.name)]  # type: ignore


def _prepare(src_dir: Path, tmp: Path) -> tuple[Path, Path, Path]:
    """复制样本到临时目录，返回 (source, success, failed)。"""
    src = tmp / "source"
    success = tmp / "success"
    failed = tmp / "failed"
    src.mkdir(parents=True)
    success.mkdir(parents=True)
    failed.mkdir(parents=True)
    for f in _pick_samples():
        shutil.copy2(f, src / f.name)
    return src, success, failed


# ── 演示函数 ──


def run_demo(
    label: str,
    src: Path,
    success: Path,
    failed: Path,
    **kwargs,
) -> BatchResult:
    """执行一次批量处理，打印回调明细。"""
    entries: list[tuple[str, str, float, str, int, int]] = []
    t0 = time.perf_counter()

    def on_entry(old: str, new: str, elapsed: float, outcome: str, index: int, total: int) -> None:
        old_name = Path(old).name
        new_name = Path(new).name if new else "(none)"
        marker = " → " if old_name != new_name else " ＝ "
        print(f"    [{index:>3}/{total}] {outcome:<15} {elapsed:>6.2f}s  {old_name}{marker}{new_name}")
        entries.append((old, new, elapsed, outcome, index, total))

    cfg = BatchConfig(**kwargs)
    processor = Batch(src, success, failed, config=cfg, entries_callback=on_entry)
    result = processor.run()
    wall = time.perf_counter() - t0

    # ── 打印结果 ──
    print(f"\n{'=' * 70}")
    print(f"  {label}")
    print(f"  {'=' * 66}")
    print(f"  结果统计: {result}")
    print(f"  墙钟耗时: {wall:.2f}s")
    print(f"  回调数量: {len(entries)}  (应与 processed={result.processed} 一致)")
    print()

    if entries:
        print(f"  {'结果分类':<15} {'耗时':>6}  源文件 → 目标文件")
        print(f"  {'-' * 15} {'-' * 6}  {'-' * 60}")
        for old, new, elapsed, outcome, index, total in entries:
            old_name = Path(old).name
            new_name = Path(new).name if new else "(none)"
            print(f"  [{index:>3}/{total}] {outcome:<15} {elapsed:>6.2f}s  {old_name} → {new_name}")
    else:
        print("  (无回调)")

    print(f"{'=' * 70}\n")
    return result


# ── 主入口 ──


def main():
    print("=" * 70)
    print("  ISBNx 批量处理  —  真实运行演示")
    print(f"  样本文件数: {len(_pick_samples())}")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="isbnx_demo_") as tmpdir:
        tmp = Path(tmpdir)

        # ── 演示 1：默认参数（rename_mode=3，skip_isbn=True）──
        s1, ok1, fail1 = _prepare(DATA_DIR, tmp / "demo1")
        run_demo("演示 1：默认参数 (rename_mode=3, skip_isbn=True)", s1, ok1, fail1)

        # ── 演示 2：skip_isbn=False（文件名有 ISBN 也做内容提取）──
        s2, ok2, fail2 = _prepare(DATA_DIR, tmp / "demo2")
        run_demo(
            "演示 2：skip_isbn=False",
            s2,
            ok2,
            fail2,
            skip_isbn=False,
        )

        # ── 演示 3：keep_name=False（仅用标识命名）──
        s3, ok3, fail3 = _prepare(DATA_DIR, tmp / "demo3")
        run_demo(
            "演示 3：keep_name=False",
            s3,
            ok3,
            fail3,
            keep_name=False,
        )

        # ── 演示 4：rename_mode=1（末尾追加，保留旧标识）──
        s4, ok4, fail4 = _prepare(DATA_DIR, tmp / "demo4")
        run_demo(
            "演示 4：rename_mode=1 (末尾追加, 保留旧标识)",
            s4,
            ok4,
            fail4,
            rename_mode=1,
        )

        # ── 演示 5：epub 只 ──
        s5, ok5, fail5 = _prepare(DATA_DIR, tmp / "demo5")
        run_demo(
            "演示 5：仅处理 .epub 文件",
            s5,
            ok5,
            fail5,
            extensions={".epub"},
        )

        # ── 演示 6：max_workers=1 串行 ──
        s6, ok6, fail6 = _prepare(DATA_DIR, tmp / "demo6")
        run_demo(
            "演示 6：串行处理 (max_workers=1)",
            s6,
            ok6,
            fail6,
            max_workers=1,
        )

    print("所有演示完成。")


if __name__ == "__main__":
    main()
