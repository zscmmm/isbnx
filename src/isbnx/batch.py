"""批量 ISBN 提取与文件整理。

扫描目录树，对每个支持的文件调用 :func:`isbnx.isbnx.extract` 提取 ISBN，
将成功/失败的文件分类移动到指定目录，成功文件自动追加 ISBN 到文件名。

功能特性：

- **多线程并行处理** — ThreadPoolExecutor，自动适配 CPU 核数
- **文件名预检** — 文件名已有 ISBN/SSID 的跳过内容提取，大幅提速
- **4 种重命名模式** — 追加/前置、替换/保留旧标识，灵活控制
- **进度条** — tqdm 实时显示进度，可独立开关
- **文件去重** — 按大小和头部指纹初筛，完整哈希确认重复内容
- **干运行模式** — 先预览操作，确认后再实际移动
- **CSV 报告** — 可选输出详细处理记录

用法示例::

    from isbnx.batch import Batch

    processor = Batch(
        source_dir="D:/books",
        success_dir="D:/books/done",
        failed_dir="D:/books/unrecognized",
    )
    result = processor.run()
    print(result)
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tqdm import tqdm

from isbnx.isbnx import ISBNX
from isbnx.utils.filename import extract_from_stem

# ── 线程局部引擎 ──
# 每个线程持有独立的 ISBNX 实例（含独立 ONNX session），
# 避免多线程共享 ONNX session 的内部锁争抢。
_thread_local = threading.local()

if TYPE_CHECKING:
    from isbnx.config import Settings
    from isbnx.models import BookInfo, ExtractResult

# ── 常量 ──

DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    ".node_modules",
    "__pycache__",
    ".venv",
    ".env",
    ".idea",
    ".vscode",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".gitignore",
    ".gitattributes",
})
"""默认跳过的目录名集合。"""

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf",
    ".epub",
    ".mobi",
    ".zip",
    ".rar",
    ".uvz",
    ".7z",
})
"""支持 ISBN 提取的文件后缀集合。"""


def _default_workers() -> int:
    """默认线程数：CPU 核数 ÷ ONNX 内部线程数（最少 2）。

    ``num_threads=1`` 时每个 session 只用 1 个 CPU 线程，
    worker 数可接近 CPU 核数。若 ``num_threads`` 较大则自动缩减。

    注意：此值仅用于**需要 ONNX 推理的文件**（PDF/archive/image 内容提取）。
    文件名含 ISBN/SSID 的跳过文件在主线程中直接处理，不受此限制。
    """
    from isbnx.config import settings

    cpus = os.cpu_count() or 4
    onnx_threads = settings.detector.num_threads
    # 每个 worker 用 onnx_threads 个 ONNX 线程，预留 1 核给系统
    return max(2, (cpus - 1) // max(onnx_threads, 1))


# ── 结果模型 ──


@dataclass
class BatchResult:
    """批量处理结果统计。"""

    # ── 计数 ──
    scanned_total: int = 0  # 扫描到的总文件数（去重前）
    total: int = 0  # 实际待处理的文件数（去重后）
    skipped: int = 0  # 文件名已有 ISBN/SSID，跳过识别直接移动
    success: int = 0  # 最终认定为成功的文件数
    failed: int = 0  # 无结果，移入 failed_dir
    isbn_count: int = 0  # 其中：内容提取到 ISBN 并追加
    ssid_count: int = 0  # 其中：内容提取到 SSID 并追加
    isbn_in_name: int = 0  # 其中：文件名已有 ISBN，跳过识别
    ssid_skipped: int = 0  # 其中：文件名已有 SSID，跳过识别
    ssid_in_name: int = 0  # 其中：文件名已有 SSID，保留原名移动
    error_preview: int = 0  # 干运行中预览异常文件移入 failed_dir 的文件数
    error_moved: int = 0  # 捕获异常并移入 failed_dir 的文件数
    error_unmoved: int = 0  # 捕获异常且未能移动的文件数

    # ── 耗时 ──
    elapsed: float = 0.0  # 总墙钟耗时（秒，含扫描/去重/移动/报告/线程调度）
    extract_elapsed_total: float = 0.0  # 各文件实际提取耗时累加（秒）

    # ── 去重 ──
    dedup_enabled: bool = False
    duplicates: int = 0
    dedup_saved_bytes: int = 0

    # ── 条目数限制 ──
    max_entries: int = 0  # 0 表示不限制
    entries_truncated: bool = False  # entries 是否被截断
    total_entries: int = 0  # 实际总条目数（截断前），0 表示未截断

    # ── 统一条目（前端直接使用，含分类标记） ──
    entries: list[tuple[str, str, float, str]] = field(default_factory=list)

    # ── 进度回调（由 Api 层设置，供前端轮询） ──
    progress_callback: Callable[[int, int, str], None] | None = None
    entries_callback: Callable[[str, str, float, str], None] | None = None

    # ── 异常 ──
    errors: list[tuple[Path, str]] = field(default_factory=list)

    # ── 详细路径列表（old → new，默认不显示） ──
    paths_skipped: list[tuple[Path, Path, float]] = field(default_factory=list)
    paths_isbn: list[tuple[Path, Path, float]] = field(default_factory=list)
    paths_ssid: list[tuple[Path, Path, float]] = field(default_factory=list)
    paths_ssid_name: list[tuple[Path, Path, float]] = field(default_factory=list)
    paths_failed: list[tuple[Path, Path, float]] = field(default_factory=list)
    paths_error_preview: list[tuple[Path, Path, str]] = field(default_factory=list)
    paths_error_moved: list[tuple[Path, Path, str]] = field(default_factory=list)
    paths_duplicates: list[tuple[Path, str]] = field(default_factory=list)

    # ── 属性 ──

    @property
    def processed(self) -> int:
        """实际尝试提取的文件数（不含跳过）。"""
        return self.success + self.failed

    @property
    def avg_wall_elapsed(self) -> float:
        """平均每文件墙钟耗时（秒，不含跳过）。"""
        n = self.processed
        return self.elapsed / n if n > 0 else 0.0

    @property
    def avg_extract_elapsed(self) -> float:
        """平均每文件纯提取耗时（秒，不含跳过）。"""
        n = self.processed
        return self.extract_elapsed_total / n if n > 0 else 0.0

    # ── 格式化 ──

    @staticmethod
    def _format_size(bytes_: int) -> str:
        """将字节数格式化为可读字符串。"""
        if bytes_ > 1024 * 1024:
            return f"{bytes_ / 1024 / 1024:.1f}MB"
        if bytes_ > 1024:
            return f"{bytes_ / 1024:.1f}KB"
        return f"{bytes_}B"

    def summary_parts(self) -> list[str]:
        """生成用于日志和 CSV 的统计片段列表。"""
        parts: list[str] = []

        # 扫描
        scan_detail = f"扫描={self.scanned_total}"
        if self.dedup_enabled:
            scan_detail += f"(去重={self.duplicates})"
        parts.append(scan_detail)

        # 总计（去重后待处理）
        parts.append(f"总计={self.total}")

        # 跳过
        skip_detail = f"跳过={self.skipped}"
        skip_sub = []
        if self.isbn_in_name:
            skip_sub.append(f"文件名ISBN={self.isbn_in_name}")
        if self.ssid_skipped:
            skip_sub.append(f"文件名SSID={self.ssid_skipped}")
        if skip_sub:
            skip_detail += f"({','.join(skip_sub)})"
        parts.append(skip_detail)

        # 成功
        success_detail = f"成功={self.success}"
        success_sub = []
        if self.isbn_count:
            success_sub.append(f"内容ISBN={self.isbn_count}")
        if self.ssid_count:
            success_sub.append(f"内容SSID={self.ssid_count}")
        if self.ssid_in_name:
            success_sub.append(f"文件名SSID={self.ssid_in_name}")
        if success_sub:
            success_detail += f"({','.join(success_sub)})"
        parts.append(success_detail)

        # 失败
        parts.append(f"失败={self.failed}")

        # 去重省空间
        if self.dedup_enabled:
            parts.append(f"去重={self.duplicates}(省{self._format_size(self.dedup_saved_bytes)})")

        # 异常
        if self.error_moved:
            parts.append(f"异常已移入失败目录={self.error_moved}")
        if self.error_unmoved:
            parts.append(f"异常未移动={self.error_unmoved}")
        elif self.error_preview:
            parts.append(f"异常预览={self.error_preview}")

        # 耗时
        parts.append(f"耗时={self.elapsed:.1f}s")
        parts.append(f"均耗时={self.avg_wall_elapsed:.2f}s")
        if self.extract_elapsed_total:
            parts.append(f"均提取={self.avg_extract_elapsed:.2f}s")

        return parts

    # ── 显示 ──

    def show_paths(self, include_skipped: bool = False) -> None:
        """打印所有文件的详细路径分类（old → new）。

        Args:
            include_skipped: 是否展示跳过项，默认 ``False``。
        """
        sections: list[tuple[str, list]] = [
            ("📗 ISBN追加", self.paths_isbn),
            ("📘 SSID追加", self.paths_ssid),
            ("📘 文件名有SSID", self.paths_ssid_name),
            ("❌ 失败", self.paths_failed),
        ]
        if include_skipped and self.paths_skipped:
            sections.insert(0, ("⏭ 跳过(文件名有ISBN/SSID)", self.paths_skipped))
        if self.paths_duplicates:
            sections.append(("🔁 内容重复", [(p, p, 0.0) for p, _ in self.paths_duplicates]))
        if self.paths_error_preview:
            sections.append(("💥 异常(预览移入失败目录)", [(p, d, 0.0) for p, d, _ in self.paths_error_preview]))
        if self.paths_error_moved:
            sections.append(("💥 异常(已移入失败目录)", [(p, d, 0.0) for p, d, _ in self.paths_error_moved]))
        for label, paths in sections:
            if paths:
                print(f"\n{label} ({len(paths)}):")
                for old, new, _elapsed in paths:
                    print(f"  {old.name} → {new}")

    # ── repr / str ──

    def __repr__(self) -> str:
        return (
            f"BatchResult(scanned={self.scanned_total}, total={self.total}, "
            f"skipped={self.skipped}, success={self.success}, failed={self.failed}, "
            f"isbn_count={self.isbn_count}, ssid_count={self.ssid_count}, "
            f"isbn_in_name={self.isbn_in_name}, ssid_skipped={self.ssid_skipped}, "
            f"ssid_in_name={self.ssid_in_name}, "
            f"dedup_enabled={self.dedup_enabled}, duplicates={self.duplicates}, "
            f"dedup_saved={self.dedup_saved_bytes}B, "
            f"wall={self.elapsed:.1f}s, avg_wall={self.avg_wall_elapsed:.2f}s, "
            f"errors={len(self.errors)}, error_preview={self.error_preview}, "
            f"error_moved={self.error_moved}, error_unmoved={self.error_unmoved})"
        )

    def __str__(self) -> str:
        return "  ".join(self.summary_parts())


# ── 处理器 ──


class Batch:
    """批量 ISBN 提取与文件整理器。

    按 ``recursive`` 参数扫描指定目录，对每个支持的格式文件提取 ISBN，
    将结果分类移动到目标目录。

    Args:
        source_dir:
            待扫描的源目录。所有支持的格式文件将被扫描并处理。
        success_dir:
            ISBN 提取成功的文件移动到此目录。
            文件名会自动追加 ISBN（规则由 ``rename_mode`` 控制）。
        failed_dir:
            ISBN 提取失败（无有效 ISBN/SSID）的文件移动到此目录。

        extensions:
            要处理的文件后缀集合（如 ``{".pdf", ".epub"}``）。
            必须为 :data:`SUPPORTED_EXTENSIONS` 的子集。默认 ``None`` 表示处理所有 6 种支持的类型。

        exclude_dirs:
            要跳过的目录名集合，默认排除 ``.git``、``__pycache__``、``.venv`` 等常见目录。
            设为 ``set()`` 可不清洗任何目录。

        max_workers:
            并行处理线程数。
            默认 ``os.cpu_count() // settings.detector.num_threads``（最少 1），
            即考虑 ONNX 内部线程数后的合理并行度。
            ONNX 每个 session 已用 ``intra_op_num_threads`` 做内部并行，
            开太多 session 会导致 CPU 超订、上下文切换变慢。

        recursive:
            是否递归扫描子目录，默认 ``True``。

        engine:
            复用的 :class:`~isbnx.isbnx.ISBNX` 实例。
            传入已初始化的实例可避免重复加载 ONNX 模型和 OCR 引擎。
            未传时自动创建新实例。
        config:
            可选的全局配置覆盖，仅 ``engine`` 未传时生效。
            通过 :func:`isbnx.config.configure` 传入。

        rename_mode:
            重命名模式（共 4 种）:

            - ``1`` — **末尾追加**：保留原文件名，在末尾追加 ``_ISBN``。
            - ``2`` — **最前面追加**：保留原文件名，在最前面插入 ``ISBN_``。
            - ``3`` — **替换旧标识再末尾追加**（默认）：用新标识替换文件名中已有的旧 ISBN/SSID，
              然后在末尾追加新标识。
            - ``4`` — **替换旧标识再最前面追加**：与模式 3 类似，但新标识插入在最前面。

            模式 1/2 中如果文件名已有标识则不重复添加。

        extensions:
            要处理的文件后缀集合（如 ``{{".pdf", ".epub"}}``）。
            必须为 ``SUPPORTED_EXTENSIONS`` 的子集。默认 ``None`` 表示处理所有 6 种支持的类型。

        pdf_front_start/pdf_front_end:
            PDF **前部**搜索范围（页码偏移，1-indexed）。
            默认从第 2 页搜到第 10 页（版权页区域）。
        pdf_back_start/pdf_back_end:
            PDF **后部**搜索范围（距末尾的偏移）。
            默认从倒数第 5 页搜到倒数第 1 页（封底区域）。

        skip_isbn:
            文件名中**已有 ISBN** 时是否跳过内容提取，默认 ``True``。
            启用可大幅提速（无需读取文件内容即可分类）。
        skip_ssid:
            文件名中**已有 SSID** 时是否跳过内容提取，默认 ``False``。
            SSID 是超星压缩包特有编号，文件名中的 SSID 不如 ISBN 可靠。

        normalize_ext:
            是否统一文件后缀为小写，默认 ``True``。
            例如 ``.PDF`` → ``.pdf``，避免大小写混用造成的混乱。

        keep_name:
            是否保留原文件名中的书名部分，默认 ``True``。
            - ``True``：保留原文件名，追加 ISBN（如 ``三体_9787536692930.pdf``）。
            - ``False``：仅用 ISBN/SSID 命名，舍弃原文件名（如 ``9787536692930.pdf``）。

        quiet:
            安静模式，不逐文件打印日志，默认 ``False``。
            与 ``show_progress`` **完全独立**，四种组合均可：

            - ``quiet=False, show_progress=True``（默认）— 既有日志又有进度条
            - ``quiet=True,  show_progress=False`` — 既无日志也无进度条
            - ``quiet=True,  show_progress=True``  — 仅进度条，无逐文件日志
            - ``quiet=False, show_progress=False`` — 仅日志，无进度条

        show_progress:
            是否显示 tqdm 进度条，默认 ``True``。
            与 ``quiet`` **完全独立**，见 ``quiet`` 的四种组合说明。

        keep_tree:
            是否在输出目录中保留源目录的子目录结构，默认 ``False``。

            - ``False``（默认）：所有输出文件平铺到 ``success_dir`` / ``failed_dir``。
            - ``True``：保留相对路径，如 ``source/小说/三体.epub`` → ``success/小说/三体_ISBN.epub``。

        deduplicate:
            是否对内容完全相同的文件去重，默认 ``False``。
            启用后，先用文件大小 + 文件头部哈希快速初筛，
            再用完整 MD5 确认重复文件，仅保留第一个。
            去重结果会记录在 ``BatchResult.paths_duplicates`` 中。

        dedup_read_size:
            去重时读取文件头部的字节数用于哈希对比，默认 ``4096``（4KB）。

            - ``0`` — 跳过头部初筛，同尺寸文件直接做完整哈希确认
            - ``4096``（4KB）— 日常均衡，先用头部指纹减少完整哈希次数
            - ``16384``（16KB）— 头部初筛更严格，I/O 开销略高
            - ``262144``（256KB）— 头部初筛更强，但 I/O 开销明显

        max_name_len:
            生成的文件名最大长度（含后缀），默认 ``180``。
            超长部分会被截断，避免文件系统（如 Windows 260 字符限制）报错。

        report_path:
            可选，指定 CSV 报告的保存路径。
            报告包含三列：分类、原路径、新路径，末尾有汇总行。

        dry_run:
            干运行模式，默认 ``False``。
            ``True`` 时仅打印日志预览操作，**不实际移动任何文件**。
            首次使用建议先 ``dry_run=True`` 确认效果。

        shutdown_event:
            可选的 ``threading.Event`` 对象，用于从外部优雅终止批量处理。
            默认 ``None`` 表示不启用关闭机制。

            用法：创建一个 ``threading.Event``，在需要取消时调用 ``event.set()``。
            处理过程中会在文件名预检、线程池提交等关键节点检查此事件，
            触发后立即停止新任务的提交，已提交正在执行的任务等待完成
            （不强制中断线程），然后返回已处理的结果统计。

        progress_callback:
            可选的进度回调函数，格式 ``Callable[[int, int, str], None]``。
            默认 ``None`` 表示不启用进度回调。

            签名 ``(processed: int, total: int, filename: str) -> None``。
            每处理完一个文件后调用，三个参数分别为：已处理文件数、总文件数、
            当前文件名。可用于驱动进度条控件。

        entries_callback:
            可选的逐条结果回调函数，格式 ``Callable[[str, str, float, str], None]``。
            默认 ``None`` 表示不启用逐条回调。

            签名 ``(old_path: str, new_path: str, elapsed: float, outcome: str) -> None``。
            每处理完一个文件后调用，四个参数分别为：原路径、新路径、耗时（秒）、
            结果分类。可用于实时同步处理结果到外部系统（如数据库、UI 列表）。

        max_entries:
            ``result.entries`` 列表的最大条目数，默认 ``1000``。
            设为 ``0`` 或负数表示不限制。
            当处理大量文件时，限制 entries 大小可避免返回数据过大。
            注意：此限制仅影响 ``entries`` 列表，不影响 ``paths_*`` 等详细路径列表。

        remove_empty_dirs:
            处理完成后是否删除源目录下的空目录，默认 ``False``。
            启用后，从叶子到根自底向上扫描源目录树，
            删除所有不包含任何文件（含子目录中文件）的空目录。
            仅在 ``dry_run=False`` 时实际删除；干运行模式下仅打印日志。
            注意：源目录本身即使变空也不会被删除。
    """

    def __init__(
        self,
        source_dir: str | Path,
        success_dir: str | Path,
        failed_dir: str | Path,
        *,
        extensions: Iterable[str] | None = None,
        exclude_dirs: set[str] | None = None,
        max_workers: int | None = None,
        recursive: bool = True,
        engine: ISBNX | None = None,
        config: Settings | None = None,
        rename_mode: int = 3,
        pdf_front_start: int | None = None,
        pdf_front_end: int | None = None,
        pdf_back_start: int | None = None,
        pdf_back_end: int | None = None,
        skip_isbn: bool = True,
        skip_ssid: bool = False,
        normalize_ext: bool = True,
        keep_name: bool = True,
        quiet: bool = False,
        show_progress: bool = True,
        keep_tree: bool = False,
        deduplicate: bool = False,
        dedup_read_size: int = 4096,
        max_name_len: int = 180,
        report_path: str | Path | None = None,
        dry_run: bool = False,
        shutdown_event: threading.Event | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
        entries_callback: Callable[[str, str, float, str], None] | None = None,
        max_entries: int = 1000,
        remove_empty_dirs: bool = False,
    ) -> None:
        # ── 参数校验 ──
        if rename_mode not in (1, 2, 3, 4):
            raise ValueError(f"rename_mode 必须是 1/2/3/4，收到 {rename_mode!r}")
        if max_workers is not None and max_workers < 1:
            raise ValueError(f"max_workers 必须 >= 1，收到 {max_workers!r}")
        if dedup_read_size < 0:
            raise ValueError(f"dedup_read_size 不能为负数，收到 {dedup_read_size!r}")
        if max_name_len < 10:
            raise ValueError(f"max_name_len 至少为 10，收到 {max_name_len!r}")
        if max_entries < 0:
            raise ValueError(f"max_entries 不能为负数，收到 {max_entries!r}")
        if not source_dir:
            raise ValueError("source_dir 不能为空")
        if not success_dir:
            raise ValueError("success_dir 不能为空")
        if not failed_dir:
            raise ValueError("failed_dir 不能为空")
        if extensions is not None:
            ext_set = set(extensions)
            invalid = ext_set - SUPPORTED_EXTENSIONS
            if invalid:
                raise ValueError(f"不支持的扩展名: {invalid}，仅支持: {sorted(SUPPORTED_EXTENSIONS)}")
            self._extensions = frozenset(ext_set)
        else:
            self._extensions = SUPPORTED_EXTENSIONS

        self.shutdown_event = shutdown_event
        self.source_dir = Path(source_dir)
        self.success_dir = Path(success_dir)
        self.failed_dir = Path(failed_dir)
        self._source_resolved: Path | None = None
        try:
            self._source_resolved = self.source_dir.resolve()
        except OSError:
            pass
        self.exclude_dirs = set(exclude_dirs) if exclude_dirs is not None else set(DEFAULT_EXCLUDE_DIRS)
        self.max_workers = max_workers if max_workers is not None else _default_workers()
        self.recursive = recursive
        self.dry_run = dry_run
        self.progress_callback = progress_callback
        self.entries_callback = entries_callback
        self.rename_mode = rename_mode
        self.skip_isbn = skip_isbn
        self.skip_ssid = skip_ssid
        self.normalize_ext = normalize_ext
        self.keep_name = keep_name
        self.quiet = quiet
        self.show_progress = show_progress
        self.keep_tree = keep_tree
        self.deduplicate = deduplicate
        self.dedup_read_size = dedup_read_size
        self.max_name_len = max_name_len
        self.report_path = Path(report_path) if report_path else None
        self.max_entries = max_entries
        self.remove_empty_dirs = remove_empty_dirs
        # 构造 PDF 配置覆盖（仅保留非 None 字段）
        pdf_overrides: dict[str, int] = {}
        if pdf_front_start is not None:
            pdf_overrides["front_start"] = pdf_front_start
        if pdf_front_end is not None:
            pdf_overrides["front_end"] = pdf_front_end
        if pdf_back_start is not None:
            pdf_overrides["back_start"] = pdf_back_start
        if pdf_back_end is not None:
            pdf_overrides["back_end"] = pdf_back_end
        from isbnx.config import PDFConfig

        self._pdf_config: PDFConfig | None = PDFConfig(**pdf_overrides) if pdf_overrides else None
        # 优先复用外部引擎，否则新建（ONNX/OCR 只预热一次）
        self._engine: ISBNX = engine or ISBNX(config=config)
        # 预计算输出目录 resolved 集合及其 basename，_scan_files 中快速过滤
        self._out_dirs_resolved: set[Path] = set()
        self._out_dir_names: set[str] = set()
        for _out in (self.success_dir, self.failed_dir):
            try:
                rp = _out.resolve()
                self._out_dirs_resolved.add(rp)
                self._out_dir_names.add(rp.name)
            except (OSError, ValueError):
                pass
        self._deleted_duplicates: list[tuple[Path, int]] = []  # (path, size_bytes)

    # ── 快速失败路径 ──

    @staticmethod
    def _failed_dst(file_path: Path, failed_dir: Path, *, normalize_ext: bool) -> Path:
        """为失败/异常文件构建移入 failed_dir 的目标路径。"""
        name = file_path.name
        if normalize_ext:
            p = Path(name)
            ls = p.suffix.lower()
            if p.suffix != ls:
                name = p.with_suffix(ls).name
        return failed_dir / name

    # ── 结果记录 ──

    @staticmethod
    def _record_outcome(
        result: BatchResult,
        fp: Path,
        outcome: str,
        dst: Path,
        elapsed: float,
        *,
        pbar: tqdm,
        max_entries: int = 1000,
    ) -> None:
        """将单个文件的结果记录到 ``result`` 并更新进度条。"""
        record = (fp, dst, elapsed)
        # 判断 entries 列表是否还能追加，同时累计总条目数
        _can_add_entry = max_entries <= 0 or len(result.entries) < max_entries
        icons: dict[str, str] = {
            "isbn_skipped": "⏭",
            "ssid_skipped": "⏭",
            "isbn_appended": "📗",
            "ssid_appended": "📘",
            "ssid_in_name": "📘",
            "error_preview": "💥",
            "error": "💥",
        }
        pbar.set_description(f"{icons.get(outcome, '❌')} {fp.name}")

        if outcome == "isbn_skipped":
            result.skipped += 1
            result.isbn_in_name += 1
            result.paths_skipped.append(record)
            if _can_add_entry:
                result.entries.append((str(fp), str(dst), elapsed, "isbn_skipped"))
        elif outcome == "ssid_skipped":
            result.skipped += 1
            result.ssid_skipped += 1
            result.paths_skipped.append(record)
            if _can_add_entry:
                result.entries.append((str(fp), str(dst), elapsed, "ssid_skipped"))
        elif outcome == "isbn_appended":
            result.success += 1
            result.isbn_count += 1
            result.paths_isbn.append(record)
            if _can_add_entry:
                result.entries.append((str(fp), str(dst), elapsed, "isbn_ok"))
            result.extract_elapsed_total += elapsed
        elif outcome == "ssid_appended":
            result.success += 1
            result.ssid_count += 1
            result.paths_ssid.append(record)
            if _can_add_entry:
                result.entries.append((str(fp), str(dst), elapsed, "ssid_ok"))
            result.extract_elapsed_total += elapsed
        elif outcome == "ssid_in_name":
            result.success += 1
            result.ssid_in_name += 1
            result.paths_ssid_name.append(record)
            if _can_add_entry:
                result.entries.append((str(fp), str(dst), elapsed, "ssid_name_ok"))
        elif outcome == "error_preview":
            result.failed += 1
            result.error_preview += 1
            result.paths_error_preview.append((fp, dst, ""))
        elif outcome == "error":
            result.failed += 1
            if dst and dst != Path():
                result.error_moved += 1
                result.paths_error_moved.append((fp, dst, ""))
            else:
                result.error_unmoved += 1
                result.errors.append((fp, "异常且无法移入失败目录"))
        else:  # "failed" 等
            result.failed += 1
            result.paths_failed.append(record)
            if _can_add_entry:
                result.entries.append((str(fp), str(dst), elapsed, "failed"))
            result.extract_elapsed_total += elapsed

        # 进度回调
        cb = result.progress_callback
        if cb:
            processed = result.skipped + result.success + result.failed
            cb(processed, result.total, fp.name)

        # 条目实时回调（增量同步）
        ecb = result.entries_callback
        if ecb and result.entries:
            last = result.entries[-1]
            ecb(last[0], last[1], last[2], last[3])

    # ── 公共入口 ──

    def run(self) -> BatchResult:
        """执行批量处理，返回统计结果。

        Returns:
            包含处理统计信息的 :class:`BatchResult`。
        """
        result = BatchResult()
        result.max_entries = self.max_entries
        result.progress_callback = self.progress_callback
        result.entries_callback = self.entries_callback
        t0 = time.perf_counter()
        self._deleted_duplicates = []
        scanned = self._collect_files()
        result.scanned_total = len(scanned) + len(self._deleted_duplicates)
        result.total = len(scanned)
        result.dedup_enabled = self.deduplicate
        result.duplicates = len(self._deleted_duplicates)
        files = scanned

        if files:
            dup_info = f"(去重跳过 {result.duplicates} 个)" if self.deduplicate else ""
            msg = f"扫描到 {result.scanned_total} 个文件{dup_info}  使用 {self.max_workers} 线程"
            if not self.quiet:
                logger.info(msg)

            if self.dry_run and not self.quiet:
                logger.info("干运行模式 — 仅打印操作，不移动文件")

            # ── 预检文件名：跳过文件直接在主线程处理，仅待提取文件进线程池 ──
            skip_files: list[Path] = []
            extract_files: list[Path] = []
            for fp in files:
                try:
                    finfo = extract_from_stem(fp.stem)
                except OSError:
                    extract_files.append(fp)
                    continue
                has_isbn = bool(finfo and finfo.isbn)
                has_ssid = bool(finfo and finfo.ssid)
                if (self.skip_isbn and has_isbn) or (self.skip_ssid and has_ssid):
                    skip_files.append(fp)
                else:
                    extract_files.append(fp)

            total_files = len(files)
            total_extract = len(extract_files)

            pbar = tqdm(
                total=total_files,
                desc="处理中",
                unit="file",
                disable=not self.show_progress,
                ncols=80,
                bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
            )

            # ── 跳过文件：主线程直接处理（轻量，不碰 ONNX） ──
            for fp in skip_files:
                if self.shutdown_event and self.shutdown_event.is_set():
                    logger.info("🛑 收到关闭信号，终止文件处理")
                    remaining = len(skip_files) + len(extract_files) - pbar.n
                    pbar.update(remaining)
                    break
                try:
                    outcome, dst, elapsed = self._process_single(fp)
                    self._record_outcome(result, fp, outcome, dst, elapsed, pbar=pbar, max_entries=self.max_entries)
                except Exception as e:
                    result.errors.append((fp, str(e)))
                    result.failed += 1
                    pbar.set_description(f"💥 {fp.name}")
                    logger.error(f"处理异常 {fp.name}: {e}")
                finally:
                    pbar.update(1)

            # ── 待提取文件：提交到线程池并行处理 ──
            # 注意：settings.detector.num_threads 设为 1 时 ONNX 单线程推理，
            # 每个 worker 仅用 1 个 CPU，可与 worker 数成正比扩展，避免超订。
            if self.shutdown_event and self.shutdown_event.is_set():
                pbar.close()
                result.elapsed = time.perf_counter() - t0
                self._log_summary(result)
                self._save_report(result)
                return result

            if extract_files:
                extract_iter = iter(extract_files)
                submitted = 0

                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    # Bounded queue: 最多挂起 max_workers*3 个 future
                    max_pending = max(self.max_workers * 3, 16)
                    pending: dict[Future, Path] = {}

                    while submitted < total_extract or pending:
                        # 检查关闭信号
                        if self.shutdown_event and self.shutdown_event.is_set():
                            logger.info("🛑 收到关闭信号，终止文件处理")
                            executor.shutdown(wait=False, cancel_futures=True)
                            pending.clear()
                            remaining = total_files - pbar.n
                            pbar.update(remaining)
                            break

                        while submitted < total_extract and len(pending) < max_pending:
                            fp = next(extract_iter)
                            fut = executor.submit(self._process_single, fp)
                            pending[fut] = fp
                            submitted += 1

                        done, _ = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                        for future in done:
                            fp = pending.pop(future)
                            try:
                                outcome, dst, elapsed = future.result()
                                self._record_outcome(
                                    result, fp, outcome, dst, elapsed, pbar=pbar, max_entries=self.max_entries
                                )
                            except Exception as e:
                                result.errors.append((fp, str(e)))
                                result.failed += 1
                                pbar.set_description(f"💥 {fp.name}")
                                logger.error(f"处理异常 {fp.name}: {e}")
                            finally:
                                pbar.update(1)

            pbar.close()

        # ── 空目录清理 ──
        if self.remove_empty_dirs:
            empty_removed = self._remove_empty_dirs()
            if empty_removed and not self.quiet and not self.dry_run:
                logger.info(f"🗑 共删除 {empty_removed} 个空目录")
            elif empty_removed and not self.quiet and self.dry_run:
                logger.info(f"[DRY] 共 {empty_removed} 个空目录将被删除")

        # ── 记录去重文件 ──
        for dup_path, dup_size in self._deleted_duplicates:
            result.paths_duplicates.append((dup_path, f"{dup_size} bytes"))
            result.dedup_saved_bytes += dup_size
        if self._deleted_duplicates:
            savebytes = BatchResult._format_size(result.dedup_saved_bytes)
            if not self.quiet:
                logger.info(f"🔁 删除 {len(self._deleted_duplicates)} 个重复文件，节省 {savebytes}")

        # ── 标记截断状态 ──
        # 实际应产生 entries 的文件总数（error_preview/error 不产生 entry）
        result.total_entries = (
            result.skipped
            + result.success
            + result.failed
            - result.error_preview
            - result.error_moved
            - result.error_unmoved
        )
        result.entries_truncated = result.max_entries > 0 and result.total_entries > result.max_entries

        result.elapsed = time.perf_counter() - t0
        self._log_summary(result)
        self._save_report(result)
        return result

    # ── 文件收集 ──

    def _scan_files(self) -> list[Path]:
        """用 ``os.scandir`` 递归遍历，排除目录直接跳过。"""
        files: list[Path] = []
        stack: list[Path] = [self.source_dir]

        while stack:
            dir_path = stack.pop()
            try:
                with os.scandir(dir_path) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                # 排除隐藏/缓存目录
                                if entry.name in self.exclude_dirs:
                                    continue
                                # 先用 basename 快速过滤输出目录（避免频繁 resolve）
                                if entry.name in self._out_dir_names and self._out_dirs_resolved:
                                    try:
                                        rp = Path(entry.path).resolve()
                                    except OSError:
                                        continue
                                    if rp in self._out_dirs_resolved:
                                        continue
                                    if rp == self._source_resolved:
                                        continue
                                    stack.append(rp)
                                else:
                                    # 普通目录直接用 entry.path 构造 Path，免 resolve
                                    stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                ext = os.path.splitext(entry.name)[1].lower()
                                if ext in self._extensions:
                                    files.append(Path(entry.path))
                        except OSError:
                            continue
            except OSError:
                continue

        return files

    def _collect_files(self) -> list[Path]:
        """收集所有待处理的文件列表（可选去重 + 按路径排序）。

        使用 ``os.scandir`` 手动遍历，排除目录直接跳过、不进入。
        """
        if self.recursive:
            files = self._scan_files()
        else:
            files = []
            for entry in self.source_dir.iterdir():
                if entry.suffix.lower() in self._extensions and entry.is_file():
                    files.append(entry)

        # 按路径排序保证确定性
        files.sort()

        # 去重后重新按路径排序，保证输出顺序稳定
        if self.deduplicate and files:
            files = self._deduplicate(files)
            files.sort()

        return files

    def _content_fingerprint(self, path: Path, size: int | None = None) -> tuple[int, str]:
        """快速文件指纹：(文件字节大小, 前 N 字节的 MD5 十六进制)。

        N 由 ``dedup_read_size`` 控制，设为 0 则跳过头部指纹。
        """
        if size is None:
            size = path.stat().st_size
        if size == 0 or self.dedup_read_size == 0:
            return (size, "")
        with open(path, "rb") as f:
            head = f.read(self.dedup_read_size)
        h = hashlib.md5(head).hexdigest()
        return (size, h)

    @staticmethod
    def _full_hash(path: Path) -> str:
        """计算文件的完整 md5 哈希。"""
        h = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                block = f.read(65536)  # 64KB 分块
                if not block:
                    break
                h.update(block)
        return h.hexdigest()

    def _deduplicate(self, files: list[Path]) -> list[Path]:
        """基于文件内容去重，重复文件在非 dry-run 模式下直接删除。

        策略：
        1. 按文件大小分组
        2. 同尺寸组内按头部指纹分组（快速初筛）
        3. 同头部指纹组内用完整 md5 确认，真重复才删除

        Returns:
            去重后的文件列表；同时填充 ``self._deleted_duplicates``。
        """
        self._deleted_duplicates = []
        unique: list[Path] = []

        # 第 1 步：按文件大小分组
        size_groups: dict[int, list[Path]] = {}
        for fp in files:
            try:
                sz = fp.stat().st_size
            except OSError:
                unique.append(fp)
                continue
            size_groups.setdefault(sz, []).append(fp)

        # 第 2 步：同尺寸组内去重
        for sz, group in size_groups.items():
            if len(group) == 1:
                unique.append(group[0])
                continue

            # 按头部指纹二次分组
            head_buckets: dict[tuple[int, str], list[Path]] = {}
            for fp in group:
                try:
                    head = self._content_fingerprint(fp, sz)
                except OSError:
                    unique.append(fp)
                    continue
                head_buckets.setdefault(head, []).append(fp)

            # 同头部指纹组内用全文件 hash 确认
            for _head, bucket in head_buckets.items():
                if len(bucket) == 1:
                    unique.append(bucket[0])
                    continue
                seen: dict[str, Path] = {}
                for fp in bucket:
                    try:
                        full_h = self._full_hash(fp)
                    except OSError:
                        unique.append(fp)
                        continue
                    if full_h in seen:
                        if not self.dry_run:
                            fp.unlink()
                        elif not self.quiet:
                            logger.info(f"[DRY] 去重: {fp}")
                        self._deleted_duplicates.append((fp, sz))
                    else:
                        seen[full_h] = fp
                        unique.append(fp)

        return unique

    # ── 路径调整 ──

    def _finalize_dst(self, dst: Path, src: Path) -> Path:
        """根据 ``keep_tree`` 调整目标路径。"""
        if not self.keep_tree:
            return dst

        source_root = self._source_resolved or self.source_dir
        try:
            rel = src.resolve().relative_to(source_root)
            parent = rel.parent
        except (OSError, ValueError):
            return dst

        return dst.parent / parent / dst.name

    # ── 空目录清理 ──

    def _remove_empty_dirs(self) -> int:
        """自底向上扫描源目录树，删除所有空目录。

        Returns:
            删除的空目录数量。
        """
        removed = 0
        # topdown=False: 先处理叶子节点，再处理父目录
        for dirpath, _dirnames, _filenames in os.walk(self.source_dir, topdown=False):
            p = Path(dirpath)
            # 跳过排除目录和源根目录
            if p.name in self.exclude_dirs or p == self.source_dir:
                continue
            try:
                if not any(p.iterdir()):
                    if self.dry_run:
                        if not self.quiet:
                            logger.info(f"[DRY] 删除空目录: {p}")
                    else:
                        p.rmdir()
                        if not self.quiet:
                            logger.info(f"🗑 删除空目录: {p}")
                    removed += 1
            except OSError:
                continue
        return removed

    # ── 线程局部引擎 ──

    @staticmethod
    def _get_thread_engine() -> ISBNX:
        """获取当前线程的 ISBNX 引擎，不存在则创建。

        每个线程持有独立的 ONNX session，避免多线程共享
        ONNX Runtime 内部锁导致的串行化。
        """
        engine: ISBNX | None = getattr(_thread_local, "engine", None)
        if engine is None:
            engine = ISBNX()
            _thread_local.engine = engine
        return engine

    # ── 单文件处理 ──

    def _process_single(self, file_path: Path) -> tuple[str, Path, float]:
        """处理单个文件：提取 → 重命名 → 移动。

        Returns:
            ``(marker, dst, elapsed)`` — 标记、实际目标路径、耗时（秒）。
            异常时 marker 为 ``"error"``；dry-run 异常预览为 ``"error_preview"``。
            dst 为失败目录路径（或空 Path）。
        """
        # ── 第 1 步：检查文件名中是否已有 ISBN/SSID ──
        try:
            filename_info = extract_from_stem(file_path.stem)
        except OSError as e:
            return self._handle_process_error(file_path, f"读取文件信息失败: {e}")

        has_isbn_in_name = bool(filename_info and filename_info.isbn)
        has_ssid_in_name = bool(filename_info and filename_info.ssid)

        # ── 第 2 步：文件名预检跳过 ──
        if (self.skip_isbn and has_isbn_in_name) or (self.skip_ssid and has_ssid_in_name):
            marker = "isbn_skipped" if self.skip_isbn and has_isbn_in_name else "ssid_skipped"
            dst = self._build_rename_dst(file_path, filename_info, None)
            dst = self._finalize_dst(dst, file_path)
            if not self.quiet:
                logger.info(f"{'[DRY] ' if self.dry_run else ''}⏭ {file_path} → {dst}")
            if not self.dry_run:
                dst = self._move_file_with_conflict(file_path, dst)
            return (marker, dst, 0.0)

        # ── 第 3 步：从文件内容提取 ISBN/SSID ──
        # 使用线程局部引擎，避免多线程共享 ONNX session 的内部锁争抢
        try:
            engine = self._get_thread_engine()
            result = engine.extract(file_path, filename=False, pdf_config=self._pdf_config)
        except Exception as e:
            return self._handle_process_error(file_path, f"内容提取异常: {e}")
        elapsed = result.elapsed or 0.0

        # ── 第 4 步：确定新文件名 ──
        content_found = False
        marker = "failed"

        if result.bookinfo.isbn13:
            dst = self._build_rename_dst(file_path, filename_info, result)
            content_found = True
            marker = "isbn_appended"
        elif result.bookinfo.ssid and not has_ssid_in_name:
            dst = self._build_rename_dst(file_path, filename_info, result)
            content_found = True
            marker = "ssid_appended"
        elif has_ssid_in_name:
            dst = self._build_rename_dst(file_path, filename_info, result)
            marker = "ssid_in_name"
        else:
            # 无有效结果 → 移入失败目录
            dst = self._failed_dst(file_path, self.failed_dir, normalize_ext=self.normalize_ext)

        # ── 第 5 步：应用 keep_tree ──
        dst = self._finalize_dst(dst, file_path)

        # ── 第 6 步：移动 ──
        icon = "📗" if "isbn" in marker else ("📘" if content_found or has_ssid_in_name else "❌")
        if not self.quiet:
            logger.info(f"{'[DRY] ' if self.dry_run else ''}{icon} {file_path} → {dst}")
        if not self.dry_run:
            try:
                dst = self._move_file_with_conflict(file_path, dst)
            except Exception as e:
                return self._handle_process_error(file_path, f"移动文件失败: {e}")

        return (marker, dst, elapsed)

    def _handle_process_error(
        self,
        file_path: Path,
        error_msg: str,
    ) -> tuple[str, Path, float]:
        """处理异常：记录日志、尝试移入失败目录。

        Returns:
            与 ``_process_single`` 兼容的 ``(marker, dst, elapsed)`` 元组。
            dry-run 下 marker 为 ``"error_preview"``。
        """
        logger.error(f"处理异常 {file_path.name}: {error_msg}")
        dst: Path = Path()
        try:
            failed_name = self._failed_dst(file_path, self.failed_dir, normalize_ext=self.normalize_ext)
            dst = self._finalize_dst(failed_name, file_path)
            if self.dry_run:
                if not self.quiet:
                    logger.info(f"[DRY] 异常文件: {file_path} → {dst}")
                return ("error_preview", dst, 0.0)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst = self._move_file_with_conflict(file_path, dst)
        except Exception as move_err:
            logger.error(f"异常文件移入失败目录也失败 {file_path.name}: {move_err}")
            return ("error", Path(), 0.0)

        return ("error", dst, 0.0)

    @staticmethod
    def _clean_stem(stem: str) -> str:
        """清理多余下划线并去除首尾下划线。"""
        return re.sub(r"_+", "_", stem).strip("_")

    def _truncate_name(self, stem: str, suffix: str) -> str:
        """超长文件名截断（总长 ≤ max_name_len）。"""
        max_stem = self.max_name_len - len(suffix)
        if len(stem) > max_stem:
            stem = stem[:max_stem]
        return stem + suffix

    def _build_rename_dst(
        self,
        src: Path,
        filename_info: BookInfo | None,
        result: ExtractResult | None,
    ) -> Path:
        """根据 rename_mode 构建目标路径。"""
        stem = src.stem
        suffix = src.suffix.lower() if self.normalize_ext else src.suffix
        mode = self.rename_mode

        # 确定要追加的标识
        if result and result.bookinfo.isbn13:
            tag = result.bookinfo.isbn13
            tag_type = "isbn"
        elif result and result.bookinfo.ssid:
            tag = result.bookinfo.ssid
            tag_type = "ssid"
        elif filename_info and filename_info.isbn13:
            tag = filename_info.isbn13
            tag_type = "isbn"
        elif filename_info and filename_info.ssid:
            tag = filename_info.ssid
            tag_type = "ssid"
        else:
            return self.success_dir / (src.stem + suffix)

        # ── 不保留原文件名，仅用标识命名 ──
        if not self.keep_name:
            # 收集所有可用标识（已按优先级降序排列）
            id_parts = [tag]
            # 尝试收集另一种标识（如 ISBN 已取，则补 SSID；反之亦然）
            other_tag: str | None = None
            if tag_type == "isbn":
                if result and result.bookinfo.ssid:
                    other_tag = result.bookinfo.ssid
                elif filename_info and filename_info.ssid:
                    other_tag = filename_info.ssid
            else:
                if result and result.bookinfo.isbn13:
                    other_tag = result.bookinfo.isbn13
                elif filename_info and filename_info.isbn13:
                    other_tag = filename_info.isbn13
            if other_tag and other_tag not in id_parts:
                id_parts.append(other_tag)
            stem = "_".join(id_parts) if id_parts else src.stem
            new_name = self._truncate_name(stem, suffix)
            return self.success_dir / new_name

        # 判断标识是否已存在于文件名中
        if tag_type == "isbn" and filename_info and filename_info.isbn:
            old_tag = filename_info.isbn
        elif tag_type == "ssid" and filename_info and filename_info.ssid:
            old_tag = filename_info.ssid
        else:
            old_tag = None

        if mode == 1:
            # 末尾追加，旧标识不变。文件名已有则不重复
            if not (old_tag and tag in stem):
                stem = f"{stem}_{tag}"
            stem = stem.strip("_ ")

        elif mode == 2:
            # 最前面追加，旧标识不变。文件名已有则不重复
            if not (old_tag and tag in stem):
                stem = f"{tag}_{stem}"
            stem = stem.strip("_ ")

        elif mode == 3:
            # 替换旧标识，再末尾追加
            if old_tag:
                stem = stem.replace(old_tag, "_")
            stem = self._clean_stem(stem)
            stem = f"{stem}_{tag}".strip("_ ")

        elif mode == 4:
            # 替换旧标识，再最前面追加
            if old_tag:
                stem = stem.replace(old_tag, "_")
            stem = self._clean_stem(stem)
            stem = f"{tag}_{stem}".strip("_ ")

        else:
            stem = self._clean_stem(stem)
            stem = stem.strip("_ ")
            return self.success_dir / (stem + suffix)

        new_name = self._truncate_name(stem, suffix)
        return self.success_dir / new_name

    def _move_file_with_conflict(self, src: Path, dst: Path) -> Path:
        """移动文件，自动创建目录，处理目标冲突。

        使用 ``shutil.move`` 而非 ``Path.rename``，以支持跨文件系统/跨盘移动。

        Returns:
            移动后的实际目标路径（可能因冲突重命名而与 ``dst`` 不同）。
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 统一后缀为小写
        if self.normalize_ext and dst.suffix != dst.suffix.lower():
            dst = dst.with_suffix(dst.suffix.lower())

        # 循环递增后缀，直到不冲突
        if dst.exists():
            stem = dst.stem
            suffix = dst.suffix
            for attempt in range(1, 1000):
                dst = dst.parent / f"{stem}_{attempt:03d}{suffix}"
                if not dst.exists():
                    break
            else:
                raise FileExistsError(f"目标文件冲突过多，无法生成可用文件名: {dst}")

        shutil.move(str(src), str(dst))
        return dst

    # ── 日志 ──

    def _save_report(self, result: BatchResult) -> None:
        """保存 CSV 报告（如果配置了 report_path）。"""
        if not self.report_path:
            return
        import csv

        fieldnames = ["分类", "原路径", "新路径", "耗時(秒)"]
        rows: list[dict[str, str]] = []

        for label, paths in [
            ("跳过(文件名有ISBN/SSID)", result.paths_skipped),
            ("ISBN追加", result.paths_isbn),
            ("SSID追加", result.paths_ssid),
            ("文件名有SSID", result.paths_ssid_name),
            ("失败", result.paths_failed),
        ]:
            for old, new, elp in paths:
                rows.append({
                    "分类": label,
                    "原路径": str(old),
                    "新路径": str(new),
                    "耗時(秒)": f"{elp:.2f}" if elp else "",
                })

        # 干运行异常预览
        for src, dst, _err in result.paths_error_preview:
            rows.append({"分类": "异常(预览)", "原路径": str(src), "新路径": str(dst), "耗時(秒)": ""})

        # 异常已移动
        for src, dst, _err in result.paths_error_moved:
            rows.append({"分类": "异常(已移入失败目录)", "原路径": str(src), "新路径": str(dst), "耗時(秒)": ""})

        # 去重记录
        for p, reason in result.paths_duplicates:
            rows.append({"分类": "内容重复", "原路径": str(p), "新路径": str(p), "耗時(秒)": ""})

        # 汇总行
        rows.append({
            "分类": "汇总",
            "原路径": f"扫描 {result.scanned_total} 个文件  待处理 {result.total} 个",
            "新路径": "  ".join(result.summary_parts()),
            "耗時(秒)": f"{result.elapsed:.1f}",
        })

        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.report_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        if not self.quiet:
            logger.info(f"📄 报告已保存: {self.report_path}")

    def _log_summary(self, result: BatchResult) -> None:
        """输出处理摘要。"""
        if not self.quiet:
            logger.info("处理完成  |  " + "  |  ".join(result.summary_parts()))
