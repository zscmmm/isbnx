"""多线程 vs 多进程 并行提取性能对比测试。

用法:
    python benchmark_parallel.py
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path

# 确保能导入 isbnx
sys.path.insert(0, str(Path(__file__).parent))

# ── 多进程 Worker（必须在模块级别定义，Windows spawn 需要） ──

_MP_ENGINE: object | None = None  # lazy init per process


def _mp_worker(path_str: str) -> float:
    """子进程 worker：提取单个文件，返回耗时。"""
    global _MP_ENGINE
    if _MP_ENGINE is None:
        from isbnx import ISBNX

        _MP_ENGINE = ISBNX()

    fp = Path(path_str)
    try:
        t0 = time.perf_counter()
        _MP_ENGINE.extract(fp, filename=False)
        return time.perf_counter() - t0
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════
# 主测试
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    from isbnx.batch import Batch
    from isbnx.config import configure
    from isbnx.utils.filename import extract_from_stem

    BASE = Path(r"D:\QQ群下的文件")
    print(f"源目录: {BASE}")
    print()

    # ── 扫描并分离文件 ──
    print("扫描文件中...")
    scanner = Batch(
        source_dir=BASE,
        success_dir=BASE / "success_dir",
        failed_dir=BASE / "notfound",
        dry_run=True,
        rename_mode=3,
        quiet=True,
        show_progress=False,
        keep_tree=False,
        deduplicate=False,
    )
    all_files = scanner._collect_files()

    extract_files: list[Path] = []
    for fp in all_files:
        finfo = extract_from_stem(fp.stem)
        if not (finfo and finfo.isbn):
            extract_files.append(fp)

    print(f"  总文件: {len(all_files)}  |  需提取: {len(extract_files)}")
    pdf_n = sum(1 for f in extract_files if f.suffix.lower() == ".pdf")
    zip_n = sum(1 for f in extract_files if f.suffix.lower() in (".zip", ".rar", ".uvz"))
    epub_n = sum(1 for f in extract_files if f.suffix.lower() == ".epub")
    print(f"    其中 PDF={pdf_n}  ZIP/UVZ={zip_n}  EPUB={epub_n}")
    print()

    results: list[tuple[str, float]] = []

    # ── 辅助函数 ──

    def _make_batch(workers: int) -> Batch:
        return Batch(
            source_dir=Path(),
            success_dir=Path(),
            failed_dir=Path(),
            dry_run=True,
            rename_mode=3,
            quiet=True,
            show_progress=False,
            keep_tree=False,
            deduplicate=False,
            max_workers=workers,
        )

    def _run_serial(b: Batch, files: list[Path]) -> float:
        t0 = time.perf_counter()
        for fp in files:
            b._process_single(fp)
        return time.perf_counter() - t0

    def _run_threadpool(b: Batch, files: list[Path], workers: int) -> float:
        total = len(files)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            max_pending = max(workers * 3, 16)
            pending = {}
            submitted = 0
            file_iter = iter(files)
            while submitted < total or pending:
                while submitted < total and len(pending) < max_pending:
                    fp = next(file_iter)
                    fut = executor.submit(b._process_single, fp)
                    pending[fut] = fp
                    submitted += 1
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    pending.pop(fut)
                    _ = fut.result()
        return time.perf_counter() - t0

    def _run_processpool(files: list[Path], workers: int) -> float:
        paths = [str(fp) for fp in files]
        t0 = time.perf_counter()
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futs = [executor.submit(_mp_worker, p) for p in paths]
            for f in futs:
                f.result()
        return time.perf_counter() - t0

    # ════════════════════════════════════════════════════
    # 1. 串行基准
    # ════════════════════════════════════════════════════
    print("=" * 58)
    print("  【基准】串行处理 — 主线程，不含跳过文件")
    print("=" * 58)
    configure(detector={"num_threads": 4})
    b = _make_batch(1)
    t = _run_serial(b, extract_files)
    print(f"  串行(1t, th=4):          {t:>6.1f}s")
    results.append(("串行(1t, th4)", t))

    configure(detector={"num_threads": 1})
    b = _make_batch(1)
    t = _run_serial(b, extract_files)
    print(f"  串行(1t, th=1):          {t:>6.1f}s")
    results.append(("串行(1t, th1)", t))
    print()

    # ════════════════════════════════════════════════════
    # 2. 多线程测试
    # ════════════════════════════════════════════════════
    print("=" * 58)
    print("  【多线程】ThreadPoolExecutor（线程局部 ONNX session）")
    print("=" * 58)
    for onnx_th, workers in [(4, 2), (4, 4), (4, 8), (1, 4), (1, 8), (1, 15)]:
        configure(detector={"num_threads": onnx_th})
        b = _make_batch(workers)
        label = f"Thread({workers}w, th={onnx_th})"
        print(f"  {label}...", end=" ", flush=True)
        t = _run_threadpool(b, extract_files, workers)
        spd = results[0][1] / t
        print(f"{t:>5.1f}s  ({spd:.2f}x)")
        results.append((label, t))
    print()

    # ════════════════════════════════════════════════════
    # 3. 多进程测试
    # ════════════════════════════════════════════════════
    print("=" * 58)
    print("  【多进程】ProcessPoolExecutor（每进程独立 ISBNX）")
    print("=" * 58)
    for onnx_th, workers in [(4, 2), (4, 4), (4, 6), (4, 8), (1, 4), (1, 8)]:
        configure(detector={"num_threads": onnx_th})
        label = f"Process({workers}p, th={onnx_th})"
        print(f"  {label}...", end=" ", flush=True)
        try:
            t = _run_processpool(extract_files, workers)
            spd = results[0][1] / t
            print(f"{t:>5.1f}s  ({spd:.2f}x)")
            results.append((label, t))
        except Exception as e:
            print(f"失败: {e}")
    print()

    # ════════════════════════════════════════════════════
    # 汇总
    # ════════════════════════════════════════════════════
    print("=" * 58)
    print("  结果汇总")
    print("=" * 58)
    baseline = results[0][1]
    print(f"  {'方法':<30s} {'耗时':>7s}  {'加速比':>7s}")
    print(f"  {'-' * 46}")
    for label, t in results:
        spd = baseline / t
        print(f"  {label:<30s} {t:>6.1f}s  {spd:>6.2f}x")
    print()
    print(f"  CPU: {os.cpu_count()} 核")
    print(f"  文件: 总{len(all_files)} (跳过{len(all_files) - len(extract_files)}, 提取{len(extract_files)})")
