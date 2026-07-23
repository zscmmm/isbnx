"""结果记录。"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from isbnx.batch.config import BatchResult
from isbnx.batch.extractor import Outcome

# ── 回调标签映射 ──
_OUTCOME_TAG: dict[Outcome, str] = {
    Outcome.SKIP_ISBN: "skip_isbn",
    Outcome.SKIP_SSID: "skip_ssid",
    Outcome.FALLBACK_SSID: "fallback_ssid",
    Outcome.EXTRACT_ISBN: "isbn_ok",
    Outcome.EXTRACT_SSID: "ssid_ok",
    Outcome.FAILED: "failed",
}
"""Outcome → 回调标签的映射。"""


def record_outcome(
    result: BatchResult,
    fp: Path,
    outcome: Outcome,
    dst: Path | None,
    elapsed: float,
    *,
    error: str | None = None,
) -> tuple[str, str, float, str] | None:
    """将单个文件的处理结果记录到 ``result``。

    返回值供调用方传递给 ``entries_callback``。

    Args:
        result: 批量处理结果对象（就地修改）。
        fp: 源文件路径。
        outcome: 结果分类。
        dst: 目标路径。
        elapsed: 处理耗时（秒）。
        error: 原始异常信息，仅 ``outcome=Outcome.ERROR`` 时记录。

    Returns:
        ``(old, new, elapsed, tag)`` 元组，供回调使用。
        当 ``outcome`` 为 ``ERROR`` 时返回 ``None``（异常不触发回调）。
    """
    if outcome in (Outcome.SKIP_ISBN, Outcome.SKIP_SSID, Outcome.FALLBACK_SSID):
        result.skipped += 1
    elif outcome in (Outcome.EXTRACT_ISBN, Outcome.EXTRACT_SSID):
        result.success += 1
    elif outcome == Outcome.ERROR:
        result.failed += 1
        if dst is not None:
            result.errors.append((fp, f"{error or '未知异常'}；已移入失败目录: {dst}"))
        else:
            result.errors.append((fp, f"{error or '未知异常'}；异常且无法移入失败目录"))
        return None
    else:  # Outcome.FAILED
        result.failed += 1

    tag = _OUTCOME_TAG.get(outcome, "failed")
    return (str(fp), str(dst), elapsed, tag)


def log_summary(result: BatchResult) -> None:
    """输出处理摘要。"""
    parts = [
        f"总计={result.total}",
        f"跳过={result.skipped}",
        f"成功={result.success}",
        f"失败={result.failed}",
        f"耗时={result.elapsed:.1f}s",
    ]
    if result.errors:
        parts.append(f"异常={len(result.errors)}")
    logger.info("处理完成  |  " + "  |  ".join(parts))
