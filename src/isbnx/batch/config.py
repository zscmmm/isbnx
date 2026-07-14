"""批量处理配置与结果模型。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import field_validator

from isbnx.config import Settings

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


# ── 结果模型 ──


@dataclass
class BatchResult:
    """批量处理结果统计。

    Attributes:
        total: 待处理的文件总数。
        skipped: 文件名已有 ISBN/SSID，跳过识别直接移动的文件数。
        success: 最终认定为成功的文件数。
        failed: 无结果，移入 failed_dir 的文件数。
        elapsed: 总墙钟耗时（秒）。
        errors: 异常列表，每项为 ``(文件路径, 错误信息)``。
    """

    total: int = 0
    skipped: int = 0
    success: int = 0
    failed: int = 0
    elapsed: float = 0.0
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.success + self.failed

    def __repr__(self) -> str:
        return (
            f"BatchResult(total={self.total}, skipped={self.skipped}, "
            f"success={self.success}, failed={self.failed}, "
            f"elapsed={self.elapsed:.1f}s, errors={len(self.errors)})"
        )


# ── 配置 ──


class BatchConfig(Settings):
    """批量处理配置。

    继承 :class:`isbnx.config.Settings` 的所有提取配置字段，
    并添加批量处理专属字段。纯 Pydantic，可直接传给 ``ISBNX(config=...)``。

    Attributes:
        extensions: 要处理的文件后缀集合。默认 ``None``=处理所有支持的类型。
        exclude_dirs: 要跳过的目录名集合。
        max_workers: 并行线程数。默认 ``None``=自动适配。
        recursive: 是否递归扫描子目录，默认 ``True``。
        rename_mode: 重命名模式，1-4，默认 3。
        normalize_ext: 统一后缀为小写，默认 ``True``。
        keep_name: 保留原文件名书名部分，默认 ``True``。
        max_name_len: 文件名最大长度（含后缀），默认 180。
        skip_isbn: 文件名有 ISBN 时跳过内容提取，默认 ``True``。
        skip_ssid: 文件名有 SSID 时跳过内容提取，默认 ``False``。
        keep_tree: 保留源目录结构，默认 ``False``。
    """

    # ── 扫描 ──
    extensions: Iterable[str] | None = None
    exclude_dirs: set[str] | None = None
    max_workers: int | None = None
    recursive: bool = True

    # ── 重命名 ──
    rename_mode: int = 3
    normalize_ext: bool = True
    keep_name: bool = True
    max_name_len: int = 180

    # ── 预检 ──
    skip_isbn: bool = True
    skip_ssid: bool = False

    # ── 输出 ──
    keep_tree: bool = False

    @field_validator("rename_mode")
    @classmethod
    def _check_rename_mode(cls, v: int) -> int:
        if v not in (1, 2, 3, 4):
            raise ValueError(f"rename_mode 必须是 1/2/3/4，收到 {v!r}")
        return v

    @field_validator("max_workers")
    @classmethod
    def _check_max_workers(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(f"max_workers 必须 >= 1，收到 {v!r}")
        return v

    @field_validator("max_name_len")
    @classmethod
    def _check_max_name_len(cls, v: int) -> int:
        if v < 10:
            raise ValueError(f"max_name_len 至少为 10，收到 {v!r}")
        return v
