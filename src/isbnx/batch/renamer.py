"""重命名策略（ISBN/SSID/original/custom）。"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from isbnx.batch.config import BatchConfig

if TYPE_CHECKING:
    from isbnx.models import BookInfo, ExtractResult


class FileRenamer:
    """文件重命名器，根据 ISBN/SSID 和配置生成目标路径。

    支持 4 种重命名模式：

    - ``1`` — 末尾追加：保留原文件名，在末尾追加 ``_ISBN``。
    - ``2`` — 最前面追加：保留原文件名，在最前面插入 ``ISBN_``。
    - ``3`` — 替换旧标识再末尾追加（默认）。
    - ``4`` — 替换旧标识再最前面追加。
    """

    def __init__(self, config: BatchConfig, success_dir: Path) -> None:
        self._cfg = config
        self._success_dir = success_dir

    @staticmethod
    def _remove_tag_from_stem(stem: str, tag_digits: str) -> str:
        """从文件名主干中移除 ISBN/SSID 数字串（兼容数字间有短横线的格式）。

        ``old_tag``（如 ``9787567673120``）是纯数字，但文件名中的实际写法可能
        带短横线（如 ``978-7-5676-7312-0``），直接用 ``str.replace`` 会匹配
        不到。此方法用正则匹配数字间可选短横线的模式来定位和移除。

        Args:
            stem: 文件名主干。
            tag_digits: 纯数字 ISBN/SSID（不含分隔符）。

        Returns:
            移除标识后的文件名主干。
        """
        pattern = r"-?".join(tag_digits)
        return re.sub(pattern, "_", stem, count=1)

    @staticmethod
    def clean_stem(stem: str) -> str:
        """清理文件名中多余下划线并去除首尾下划线。

        Args:
            stem: 文件名主干（不含后缀）。

        Returns:
            清理后的文件名主干。
        """
        return re.sub(r"_+", "_", stem).strip("_")

    def truncate_name(self, stem: str, suffix: str) -> str:
        """超长文件名截断，确保总长度不超过 ``max_name_len``。

        Args:
            stem: 文件名主干（不含后缀）。
            suffix: 文件后缀（含点号，如 ``".pdf"``）。

        Returns:
            截断后的完整文件名（主干 + 后缀）。
        """
        max_stem = self._cfg.max_name_len - len(suffix)
        if len(stem) > max_stem:
            stem = stem[:max_stem]
        return stem + suffix

    @staticmethod
    def failed_dst(file_path: Path, failed_dir: Path, *, normalize_ext: bool) -> Path:
        """为失败/异常文件构建移入 failed_dir 的目标路径。

        Args:
            file_path: 源文件路径。
            failed_dir: 失败目录。
            normalize_ext: 是否统一后缀为小写。

        Returns:
            失败目录下的目标路径。
        """
        name = file_path.name
        if normalize_ext:
            p = Path(name)
            ls = p.suffix.lower()
            if p.suffix != ls:
                name = p.with_suffix(ls).name
        return failed_dir / name

    def move_file_with_conflict(self, src: Path, dst: Path) -> Path:
        """移动文件，自动创建目录并处理目标冲突。

        使用 ``shutil.move`` 而非 ``Path.rename``，以支持跨文件系统/跨盘移动。

        Args:
            src: 源文件路径。
            dst: 目标路径。

        Returns:
            移动后的实际目标路径（可能因冲突重命名而与 ``dst`` 不同）。
            冲突计数器达到上限时使用 UUID 后缀降级。
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 统一后缀为小写
        if self._cfg.normalize_ext and dst.suffix != dst.suffix.lower():
            dst = dst.with_suffix(dst.suffix.lower())

        # 循环递增后缀，直到不冲突
        if dst.exists():
            stem = dst.stem
            suffix = dst.suffix
            for attempt in range(1, 100):
                candidate = dst.parent / f"{stem}_{attempt:03d}{suffix}"
                if not candidate.exists():
                    dst = candidate
                    break
            else:
                # 计数器耗尽，使用 UUID 后缀降级以避免 TOCTOU 竞态下的静默失败
                dst = dst.parent / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"

        shutil.move(str(src), str(dst))
        return dst

    def build_rename_dst(
        self,
        src: Path,
        filename_info: BookInfo | None,
        result: ExtractResult | None,
    ) -> Path:
        """根据 ``rename_mode`` 构建重命名后的目标路径。

        Args:
            src: 源文件路径。
            filename_info: 从文件名中提取的 BookInfo（可为 None）。
            result: 内容提取结果（可为 None）。

        Returns:
            重命名后的目标路径（位于 success_dir 下）。
        """
        stem = src.stem
        suffix = src.suffix.lower() if self._cfg.normalize_ext else src.suffix
        mode = self._cfg.rename_mode

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
            return self._success_dir / (src.stem + suffix)

        # ── 不保留原文件名，仅用标识命名 ──
        if not self._cfg.keep_name:
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
            new_name = self.truncate_name(stem, suffix)
            return self._success_dir / new_name

        # 判断标识是否已存在于文件名中
        if tag_type == "isbn" and filename_info and filename_info.isbn:
            old_tag = filename_info.isbn
        elif tag_type == "ssid" and filename_info and filename_info.ssid:
            old_tag = filename_info.ssid
        else:
            old_tag = None

        if mode == 1:
            # 末尾追加，旧标识不变。文件名已含 ISBN（兼容连字符）则跳过追加
            stem_flat = stem.replace("-", "").replace(" ", "")
            if not (old_tag and tag in stem_flat):
                stem = f"{stem}_{tag}"
            stem = stem.strip("_ ")

        elif mode == 2:
            # 最前面追加，旧标识不变。文件名已含 ISBN（兼容连字符）则跳过追加
            stem_flat = stem.replace("-", "").replace(" ", "")
            if not (old_tag and tag in stem_flat):
                stem = f"{tag}_{stem}"
            stem = stem.strip("_ ")

        elif mode == 3:
            # 替换旧标识，再末尾追加
            if old_tag:
                stem = self._remove_tag_from_stem(stem, old_tag)
            stem = self.clean_stem(stem)
            stem = f"{stem}_{tag}".strip("_ ")

        elif mode == 4:
            # 替换旧标识，再最前面追加
            if old_tag:
                stem = self._remove_tag_from_stem(stem, old_tag)
            stem = self.clean_stem(stem)
            stem = f"{tag}_{stem}".strip("_ ")

        new_name = self.truncate_name(stem, suffix)
        return self._success_dir / new_name
