"""文件 ISBN 提取器。

封装线程局部 ONNX 引擎、文件名预检、内容提取等逻辑。
提取结果不含重命名/移动信息，由上层 ``Batch`` 类统一编排。
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from isbnx.utils.filename import extract_from_stem

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from isbnx.config import PDFConfig
    from isbnx.isbnx import ISBNX
    from isbnx.models import BookInfo, ExtractResult


# ── 提取结果分类 ──


class Outcome(StrEnum):
    """文件提取结果分类。"""

    # ── 跳过（文件名已有标识，未做内容提取）──
    SKIP_ISBN = "skip_isbn"
    """文件名含 ISBN，跳过内容提取。"""
    SKIP_SSID = "skip_ssid"
    """文件名含 SSID，跳过内容提取。"""

    # ── 成功（内容提取到标识）──
    EXTRACT_ISBN = "extract_isbn"
    """从文件内容提取到 ISBN-13。"""
    EXTRACT_SSID = "extract_ssid"
    """从文件内容提取到 SSID（文件名不含 SSID）。"""

    # ── 降级（内容无结果，回退到文件名中的标识）──
    FALLBACK_SSID = "fallback_ssid"
    """内容未提取到有效标识，回退使用文件名中已有的 SSID。"""

    # ── 失败／异常 ──
    FAILED = "failed"
    """内容扫描后无有效 ISBN/SSID。"""
    ERROR = "error"
    """处理过程抛出异常。"""


# ── 提取结果 ──


@dataclass
class FileResult:
    """单个文件的 ISBN 提取结果（不含重命名/移动信息）。

    Attributes:
        src: 源文件路径。
        outcome: 结果分类，:class:`Outcome` 枚举值。
        filename_info: 文件名中提取到的标识信息。
        extract_result: 内容提取的完整结果。
        elapsed: 提取耗时（秒）。
        error: 错误信息（``outcome=Outcome.ERROR`` 时）。
    """

    src: Path
    outcome: Outcome
    filename_info: BookInfo | None = None
    extract_result: ExtractResult | None = None
    elapsed: float = 0.0
    error: str | None = None


# ── 提取器 ──


class FileExtractor:
    """文件 ISBN 提取器。

    所有线程共享同一个 :class:`~isbnx.isbnx.ISBNX` 实例，
    ONNX Runtime 内部已做线程安全处理，无需每个线程新建 session。

    Args:
        base_engine:
            复用的 ISBNX 实例，所有工作线程共享此引擎。
        pdf_config:
            PDF 页码配置覆盖，为 ``None`` 时使用全局默认值。
        skip_isbn:
            文件名有 ISBN 时跳过内容提取。
        skip_ssid:
            文件名有 SSID 时跳过内容提取。
        strict:
            文件名 ISBN 解析的严格模式。
    """

    def __init__(
        self,
        base_engine: ISBNX,
        *,
        pdf_config: PDFConfig | None = None,
        skip_isbn: bool = True,
        skip_ssid: bool = False,
        strict: bool = False,
    ) -> None:
        self._engine = base_engine
        self._pdf_config = pdf_config
        self._skip_isbn = skip_isbn
        self._skip_ssid = skip_ssid
        self._strict = strict

    # ── 单文件提取 ──

    def extract_one(self, file_path: Path) -> FileResult:
        """提取单个文件的 ISBN（文件名预检 + 内容提取）。

        Args:
            file_path: 待提取的文件路径。

        Returns:
            :class:`FileResult` 包含提取结果，不含重命名/移动信息。

        Note:
            ``extract_from_stem`` 的文件名预检阶段可能抛出 ``OSError``（权限/路径
            异常），此方法会将其捕获并返回 ``Outcome.ERROR``，避免上层崩溃。
        """
        # ── 第 1 步：文件名预检 ──
        try:
            filename_info = extract_from_stem(file_path.stem, strict=self._strict)
        except OSError as e:
            return FileResult(src=file_path, outcome=Outcome.ERROR, error=f"读取文件信息失败: {e}")

        has_isbn = bool(filename_info and filename_info.isbn)
        has_ssid = bool(filename_info and filename_info.ssid)

        # ── 第 2 步：文件名已有标识，跳过内容提取 ──
        if (self._skip_isbn and has_isbn) or (self._skip_ssid and has_ssid):
            outcome = Outcome.SKIP_ISBN if (self._skip_isbn and has_isbn) else Outcome.SKIP_SSID
            return FileResult(src=file_path, outcome=outcome, filename_info=filename_info, elapsed=0.0)

        # ── 第 3 步：从文件内容提取 ISBN/SSID ──
        try:
            result = self._engine.extract(file_path, filename=False, pdf_config=self._pdf_config)
        except Exception as e:
            return FileResult(src=file_path, outcome=Outcome.ERROR, error=f"内容提取异常: {e}")

        elapsed = result.elapsed or 0.0

        # ── 第 4 步：确定 outcome ──
        if result.bookinfo.isbn13:
            outcome = Outcome.EXTRACT_ISBN
        elif result.bookinfo.ssid and not has_ssid:
            outcome = Outcome.EXTRACT_SSID
        elif has_ssid:
            outcome = Outcome.FALLBACK_SSID
        else:
            outcome = Outcome.FAILED

        return FileResult(
            src=file_path,
            outcome=outcome,
            filename_info=filename_info,
            extract_result=result,
            elapsed=elapsed,
        )

    # ── 批量提取 ──

    def extract_batch(
        self,
        files: list[Path],
        *,
        max_workers: int,
        shutdown_event: threading.Event | None = None,
        on_result: Callable[[FileResult], None] | None = None,
    ) -> list[FileResult]:
        """批量提取多个文件的 ISBN。

        文件名已有 ISBN/SSID 的跳过文件在主线程串行处理，
        需要内容提取的文件提交到线程池并行处理。
        每个文件提取完成后立即回调 ``on_result``（如有），
        调用方可边提取边处理（如移动文件），无需等全部完成。

        Args:
            files: 待提取的文件路径列表。
            max_workers: 线程池最大线程数。
            shutdown_event: 可选的关闭事件，触发后终止新任务提交。
            on_result: 每文件完成回调 ``(FileResult) -> None``。

        Returns:
            提取结果列表，顺序与输入不一定一致（跳过文件在前，并行文件随后）。

        Note:
            文件名预检阶段可能抛出任意异常（权限/编码/路径等问题），
            会被统一捕获并记入 ``extract_files``，避免单个异常阻塞整批任务。
        """
        # ── 预检分拣 ──
        skip_files: list[Path] = []
        extract_files: list[Path] = []
        for fp in files:
            try:
                finfo = extract_from_stem(fp.stem, strict=self._strict)
            except Exception:  # noqa: BLE001
                logger.debug(f"文件名预检异常，转入内容提取: {fp.name}")
                extract_files.append(fp)
                continue
            has_isbn = bool(finfo and finfo.isbn)
            has_ssid = bool(finfo and finfo.ssid)
            if (self._skip_isbn and has_isbn) or (self._skip_ssid and has_ssid):
                skip_files.append(fp)
            else:
                extract_files.append(fp)

        results: list[FileResult] = []

        def _done(fr: FileResult) -> None:
            results.append(fr)
            if on_result:
                on_result(fr)

        # ── 跳过文件：主线程直接处理 ──
        for fp in skip_files:
            if shutdown_event and shutdown_event.is_set():
                logger.info("🛑 收到关闭信号，终止文件处理")
                break
            _done(self.extract_one(fp))

        if shutdown_event and shutdown_event.is_set():
            return results

        # ── 待提取文件：线程池并行 ──
        if extract_files:
            total_extract = len(extract_files)
            extract_iter = iter(extract_files)
            submitted = 0

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                max_pending = max(max_workers * 3, 16)
                pending: dict[Future, Path] = {}

                while submitted < total_extract or pending:
                    if shutdown_event and shutdown_event.is_set():
                        logger.info("🛑 收到关闭信号，终止文件处理")
                        executor.shutdown(wait=False, cancel_futures=True)
                        pending.clear()
                        break

                    while submitted < total_extract and len(pending) < max_pending:
                        fp = next(extract_iter)
                        fut = executor.submit(self.extract_one, fp)
                        pending[fut] = fp
                        submitted += 1

                    done, _ = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                    for future in done:
                        fp = pending.pop(future)
                        try:
                            _done(future.result())
                        except Exception as e:
                            _done(FileResult(src=fp, outcome=Outcome.ERROR, error=str(e)))

        return results
