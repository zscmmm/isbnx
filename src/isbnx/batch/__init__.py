"""批量 ISBN 提取与文件整理。

扫描目录树，对每个支持的文件调用 :func:`isbnx.isbnx.extract` 提取 ISBN，
将成功/失败的文件分类移动到指定目录，成功文件自动追加 ISBN 到文件名。

功能特性：

- **多线程并行处理** — ThreadPoolExecutor，自动适配 CPU 核数
- **文件名预检** — 文件名已有 ISBN/SSID 的跳过内容提取，大幅提速
- **4 种重命名模式** — 追加/前置、替换/保留旧标识，灵活控制

用法示例::

    from isbnx.batch import Batch

    processor = Batch(
        source_dir="D:/books",
        success_dir="D:/books/done",
        failed_dir="D:/books/unrecognized",
    )
    result = processor.run()
    print(result)

    # 自定义配置
    from isbnx.batch import BatchConfig

    config = BatchConfig(strict=2, rename_mode=1)
    processor = Batch(..., config=config)
    result = processor.run()
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from isbnx.batch import reporter
from isbnx.batch.config import (
    DEFAULT_EXCLUDE_DIRS,
    SUPPORTED_EXTENSIONS,
    BatchConfig,
    BatchResult,
)
from isbnx.batch.extractor import FileExtractor, FileResult, Outcome
from isbnx.batch.renamer import FileRenamer
from isbnx.isbnx import ISBNX

__all__ = [
    "Batch",
    "BatchConfig",
    "BatchResult",
    "DEFAULT_EXCLUDE_DIRS",
    "SUPPORTED_EXTENSIONS",
]


# ── 处理器 ──


class Batch:
    """批量 ISBN 提取与文件整理器。

    按 ``recursive`` 参数扫描指定目录，对每个支持的格式文件提取 ISBN，
    将结果分类移动到目标目录。

    Args:
        source_dir:
            待扫描的源目录。
        success_dir:
            ISBN 提取成功的文件移动到此目录。
        failed_dir:
            ISBN 提取失败的文件移动到此目录。
        config:
            批量处理配置，继承 :class:`~isbnx.config.Settings` 所有提取配置字段。
            为 ``None`` 时使用全部默认配置。
        entries_callback:
            逐条结果回调 ``(old, new, elapsed, outcome, index, total) -> None``。
        shutdown_event:
            外部取消事件，触发后停止处理。
        try_run:
            试运行模式，默认 ``False``。只扫描不移动文件。
        \\**kwargs:
            其他关键字参数将被静默忽略（保留用于向后兼容）。
    """

    def __init__(
        self,
        source_dir: str | Path,
        success_dir: str | Path,
        failed_dir: str | Path,
        *,
        config: BatchConfig | None = None,
        entries_callback: Callable[[str, str, float, str, int, int], None] | None = None,
        shutdown_event: threading.Event | None = None,
        try_run: bool = False,
        **kwargs: object,
    ) -> None:
        # ── 配置 ──
        self._cfg = config or BatchConfig()
        if kwargs:
            logger.debug(f"Batch 收到未识别的参数，已忽略: {set(kwargs)}")

        # ── 运行时控制 ──
        self._entries_callback = entries_callback
        self.shutdown_event = shutdown_event
        self._try_run = try_run

        # ── 引擎（BatchConfig 是 Settings 的子类，直接传入）──
        self._engine = ISBNX(config=self._cfg)

        # ── 路径 ──
        self.source_dir = Path(source_dir)
        self.success_dir = Path(success_dir)
        self.failed_dir = Path(failed_dir)
        self._source_resolved: Path | None = None
        try:
            self._source_resolved = self.source_dir.resolve()
        except OSError:
            pass
        self.exclude_dirs = (
            set(self._cfg.exclude_dirs) if self._cfg.exclude_dirs is not None else set(DEFAULT_EXCLUDE_DIRS)
        )

        # ── 线程数 ──
        self.max_workers = (
            self._cfg.max_workers
            if self._cfg.max_workers is not None
            else max(2, ((os.cpu_count() or 4) - 1) // max(self._engine.config.detector.num_threads, 1))
        )

        # ── 扩展名 ──
        if self._cfg.extensions is not None:
            ext_set = set(self._cfg.extensions)
            invalid = ext_set - SUPPORTED_EXTENSIONS
            if invalid:
                raise ValueError(f"不支持的扩展名: {invalid}，仅支持: {sorted(SUPPORTED_EXTENSIONS)}")
            self._extensions = frozenset(ext_set)
        else:
            self._extensions = SUPPORTED_EXTENSIONS

        # ── 输出目录过滤 ──
        self._out_dirs_resolved: set[Path] = set()
        for _out in (self.success_dir, self.failed_dir):
            try:
                rp = _out.resolve()
                self._out_dirs_resolved.add(rp)
            except (OSError, ValueError):
                pass

        # ── 子模块 ──
        self._renamer = FileRenamer(self._cfg, self.success_dir)

    # ── 路径调整 ──

    def _finalize_dst(self, dst: Path, src: Path) -> Path:
        """根据 ``keep_tree`` 调整目标路径。"""
        if not self._cfg.keep_tree:
            return dst

        source_root = self._source_resolved or self.source_dir
        try:
            rel = src.resolve().relative_to(source_root)
            parent = rel.parent
        except (OSError, ValueError):
            return dst

        return dst.parent / parent / dst.name

    # ── 文件目标路径 + 移动 ──

    def _dest_and_move(self, file_path: Path, outcome: Outcome, fr: FileResult) -> tuple[Outcome, Path | None]:
        """根据提取结果为文件确定目标路径并执行移动。

        Args:
            file_path: 源文件路径。
            outcome: 提取结果分类。
            fr: 提取结果对象。

        Returns:
            ``(actual_outcome, dst)`` — 实际结果分类和目标路径。
            移动失败时 ``actual_outcome`` 为 ``Outcome.ERROR``。
        """
        if outcome == Outcome.ERROR:
            return self._error_and_move(file_path, fr.error or "未知错误")

        # 跳过文件（含 name_ssid）：纯移动，不改名
        if outcome in (Outcome.SKIP_ISBN, Outcome.SKIP_SSID, Outcome.FALLBACK_SSID):
            dst = self._renamer.build_rename_dst(file_path, fr.filename_info, None)
            dst = self._finalize_dst(dst, file_path)
            if not self._try_run:
                dst = self._renamer.move_file_with_conflict(file_path, dst)
            return (outcome, dst)

        # 成功：先算目标路径，再移动
        if outcome in (Outcome.EXTRACT_ISBN, Outcome.EXTRACT_SSID):
            dst = self._renamer.build_rename_dst(file_path, fr.filename_info, fr.extract_result)
        else:  # Outcome.FAILED
            dst = FileRenamer.failed_dst(file_path, self.failed_dir, normalize_ext=self._cfg.normalize_ext)

        dst = self._finalize_dst(dst, file_path)

        if not self._try_run:
            try:
                dst = self._renamer.move_file_with_conflict(file_path, dst)
            except Exception as e:
                return self._error_and_move(file_path, f"移动文件失败: {e}")

        return (outcome, dst)

    def _error_and_move(self, file_path: Path, error_msg: str) -> tuple[Outcome, Path | None]:
        """异常文件：记录日志、尝试移入失败目录。"""
        logger.error(f"处理异常 {file_path.name}: {error_msg}")
        if self._try_run:
            return (Outcome.ERROR, None)
        try:
            dst = FileRenamer.failed_dst(file_path, self.failed_dir, normalize_ext=self._cfg.normalize_ext)
            dst = self._finalize_dst(dst, file_path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst = self._renamer.move_file_with_conflict(file_path, dst)
            return (Outcome.ERROR, dst)
        except Exception as e:
            logger.error(f"异常文件移入失败目录也失败 {file_path.name}: {e}")
            return (Outcome.ERROR, None)

    # ── 步骤 1：扫描 ──

    def _scan_files(self) -> list[Path]:
        """扫描目录，返回待处理的文件列表。"""
        from dedupx import FileScanner  # type: ignore

        return [
            sf.filepath
            for sf in FileScanner(
                include_extensions=list(self._extensions),
                exclude_names=self.exclude_dirs,
                exclude_paths=list(self._out_dirs_resolved) if self._out_dirs_resolved else [],
                recursive=self._cfg.recursive,
                dedup_inodes=False,
            ).collect_info(self.source_dir)
        ]

    # ── 步骤 2+3+4：提取 → 重命名 → 移动（流式处理） ──

    def _process_all(self, result: BatchResult, files: list[Path]) -> None:
        """批量处理文件：边提取、边重命名、边移动。"""
        extractor = FileExtractor(
            self._engine,
            skip_isbn=self._cfg.skip_isbn,
            skip_ssid=self._cfg.skip_ssid,
            strict=self._engine.config.strict,
        )

        def _on_result(fr: FileResult) -> None:
            if self.shutdown_event and self.shutdown_event.is_set():
                return
            outcome, dst = self._dest_and_move(fr.src, fr.outcome, fr)
            entry = reporter.record_outcome(result, fr.src, outcome, dst, fr.elapsed)
            if entry and self._entries_callback:
                old, new, elapsed, tag = entry
                idx = result.success + result.failed + result.skipped
                self._entries_callback(old, new, elapsed, tag, idx, result.total)

        extractor.extract_batch(
            files,
            max_workers=self.max_workers,
            shutdown_event=self.shutdown_event,
            on_result=_on_result,
        )

    # ── 步骤 4：收尾 ──

    def _finalize(self, result: BatchResult, t0: float) -> None:
        """收尾：日志。"""
        result.elapsed = time.perf_counter() - t0
        reporter.log_summary(result)

    # ── 公共入口 ──

    def run(self) -> BatchResult:
        """执行批量处理，返回统计结果。

        Returns:
            包含处理统计信息的 :class:`BatchResult`。
        """
        result = BatchResult()
        t0 = time.perf_counter()

        # ── Step 1: 扫描 ──
        files = self._scan_files()
        result.total = len(files)

        if files:
            logger.info(f"扫描目录: {self.source_dir}, 扫描到 {result.total} 个文件  使用 {self.max_workers} 线程")

            # ── Step 2: 提取 → 重命名 → 移动（流式） ──
            self._process_all(result, files)

        # ── Step 3: 收尾 ──
        self._finalize(result, t0)
        return result
