"""分步去重模块：先扫描查找重复文件，再由前端确认后执行删除。"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Collection, Iterator
from pathlib import Path

from pydantic import BaseModel, Field, computed_field

from .md5_cache import MD5Cache

_HASH_CHUNK_SIZE = 1024 * 1024
_PREFIX_SIZE = 256
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0)
_HAS_FILE_DIGEST = hasattr(hashlib, "file_digest")

_DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    ".env",
    ".idea",
    ".vscode",
    "__pycache__",
    ".tox",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".node_modules",
    ".next",
    ".nuxt",
})

# 排序策略 → 排序键函数（共享常量，避免重复定义）
_SORT_KEY_ATTR: dict[str, str] = {
    "oldest_changed": "changed",
    "newest_changed": "changed",
    "oldest_modified": "modified",
    "newest_modified": "modified",
    "oldest_accessed": "accessed",
    "newest_accessed": "accessed",
}
_SORT_REVERSE: dict[str, bool] = {
    "oldest_changed": False,
    "newest_changed": True,
    "oldest_modified": False,
    "newest_modified": True,
    "oldest_accessed": False,
    "newest_accessed": True,
}


# ──────────────────────────── 数据模型 ────────────────────────────


class FileInfo(BaseModel):
    """单个文件的元数据信息。"""

    path: str = Field(description="文件绝对路径")
    size: int = Field(description="文件大小（字节）")
    accessed: int = Field(description="最后访问时间戳（纳秒，st_atime_ns）")
    modified: int = Field(description="最后修改时间戳（纳秒，st_mtime_ns）")
    changed: int = Field(description="元数据更改时间戳（纳秒，st_ctime_ns；Windows 上为创建时间）")
    keep: bool = Field(default=False, description="当前策略下是否保留此文件")

    def get(self, key: str, default=None):
        """类似 dict.get()，安全获取字段值，字段不存在时返回默认值。"""
        return getattr(self, key, default)


class DuplicateGroup(BaseModel):
    """一组内容相同的重复文件（仅描述重复组，不决定保留策略）。"""

    size: int = Field(description="文件大小（字节）")
    files: list[FileInfo] = Field(description="组内所有文件")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dup_count(self) -> int:
        """可删除的重复文件数量（总数 - 1）。"""
        return max(len(self.files) - 1, 0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def saved_bytes(self) -> int:
        """该组可回收的字节数。"""
        return self.size * self.dup_count


class RemoveError(BaseModel):
    """删除操作中的单个错误。"""

    file: str = Field(description="操作失败的文件路径")
    error: str = Field(description="错误信息")


class ScanResult(BaseModel):
    """find_duplicates 的扫描结果。"""

    dir: str = Field(description="扫描的根目录绝对路径")
    total: int = Field(description="扫描到的总文件数（经筛选后）")
    groups: list[DuplicateGroup] = Field(description="按大小降序排列的重复组列表")
    time_s: float = Field(description="扫描耗时（秒）")
    has_more: bool = Field(default=False, description="是否因 max_results 限制而截断了结果")
    cache_stats: dict[str, int] | None = Field(
        default=None,
        description="缓存统计 {hits, misses, stores}，未启用缓存时为 None",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dup_groups(self) -> int:
        """重复组数。"""
        return len(self.groups)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dups(self) -> int:
        """重复文件总数。"""
        return sum(g.dup_count for g in self.groups)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def saved(self) -> int:
        """重复文件占用的总字节数。"""
        return sum(g.saved_bytes for g in self.groups)


class RemoveResult(BaseModel):
    """remove_duplicates 的操作结果。"""

    removed: list[str] = Field(description="成功处理的文件路径列表")
    errors: list[RemoveError] = Field(description="处理失败的文件及其错误信息列表")
    reclaimed_bytes: int = Field(description="回收的总字节数")
    time_s: float = Field(description="操作耗时（秒）")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def removed_count(self) -> int:
        """成功处理的文件数。"""
        return len(self.removed)


class AutoCleanResult(BaseModel):
    """一键清除的完整结果（扫描 + 删除）。"""

    total_scanned: int = Field(description="扫描到的总文件数")
    dup_groups: int = Field(description="重复组数")
    dup_files: int = Field(description="重复文件数（含保留文件）")
    removed: list[str] = Field(description="成功删除的文件路径")
    errors: list[RemoveError] = Field(description="删除失败的文件及错误信息")
    reclaimed_bytes: int = Field(description="回收的总字节数")
    time_s: float = Field(description="总耗时（秒）")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def removed_count(self) -> int:
        """成功删除的文件数。"""
        return len(self.removed)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def error_count(self) -> int:
        """失败的文件数。"""
        return len(self.errors)


# ──────────────────────────── 内部工具函数 ────────────────────────────


def _normalize_root_dir(folder_path: str | Path) -> Path:
    """将目标文件夹路径规范化为绝对路径，并校验其存在性和目录类型。"""
    root_dir = Path(folder_path).expanduser().resolve()
    if not root_dir.exists():
        raise FileNotFoundError(f"Folder does not exist: {root_dir}")
    if not root_dir.is_dir():
        raise NotADirectoryError(f"Path is not a folder: {root_dir}")
    return root_dir


def _iter_files(
    root_dir: Path,
    recursive: bool,
    exclude_dirs: Collection[str] | None = None,
    include_extensions: Collection[str] | None = None,
    exclude_extensions: Collection[str] | None = None,
    min_size: int = 1,
    max_size: int | None = None,
    follow_symlinks: bool = False,
) -> Iterator[tuple[str, int]]:
    """
    递归或扁平扫描目录，按条件过滤后逐个产出 (文件路径, 文件大小)。

    过滤条件包括：排除目录、扩展名白/黑名单、文件大小范围、符号链接策略等。
    通过 (st_dev, st_ino) 去重，避免硬链接或 follow_symlinks 场景下同一物理文件被重复计算。
    """
    folders = [os.fspath(root_dir)]
    seen_inodes: set[tuple[int, int]] = set()

    while folders:
        folder = folders.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=follow_symlinks):
                            if not recursive:
                                continue
                            if exclude_dirs and entry.name in exclude_dirs:
                                continue
                            folders.append(entry.path)
                            continue

                        if not entry.is_file(follow_symlinks=follow_symlinks):
                            continue

                        stat_result = entry.stat(follow_symlinks=follow_symlinks)
                    except OSError:
                        continue

                    # inode 去重：同一物理文件只处理一次
                    # Windows 某些文件系统 (exFAT/FAT32/网络盘) st_ino 恒为 0，
                    # 此时跳过去重，否则会误将所有文件视为同一文件。
                    if stat_result.st_ino:
                        inode_key = (stat_result.st_dev, stat_result.st_ino)
                        if inode_key in seen_inodes:
                            continue
                        seen_inodes.add(inode_key)

                    file_size = stat_result.st_size
                    if file_size < min_size:
                        continue
                    if max_size is not None and file_size > max_size:
                        continue
                    if include_extensions is not None or exclude_extensions is not None:
                        dot = entry.name.rfind(".")
                        ext = entry.name[dot:].lower() if dot != -1 else ""
                        if include_extensions is not None and ext not in include_extensions:
                            continue
                        if exclude_extensions is not None and ext in exclude_extensions:
                            continue

                    yield entry.path, file_size
        except OSError:
            continue


def _resolve_workers(num_workers: int, num_files: int) -> int:
    """根据参数和实际情况决定实际工作线程数。"""
    if num_workers != 0:
        return max(1, num_workers)
    if num_files <= 1:
        return 1
    return min(os.cpu_count() or 4, num_files)


def _calculate_md5(file_path: str) -> bytes:
    """计算单个文件的完整 MD5 摘要（二进制 bytes），优先使用 hashlib.file_digest。"""
    with open(file_path, "rb") as file_obj:
        if _HAS_FILE_DIGEST:
            return hashlib.file_digest(file_obj, "md5").digest()

        md5 = hashlib.md5()
        while chunk := file_obj.read(_HASH_CHUNK_SIZE):
            md5.update(chunk)
    return md5.digest()


def _compute_md5_batch(
    file_paths: list[str],
    num_workers: int,
    cache: MD5Cache | None = None,
    cache_min_size: int = 5 * 1024 * 1024,
) -> list[bytes]:
    """批量计算 MD5，自动或指定并行度。支持可选缓存。

    当 cache 不为 None 时：
    - 对 >= cache_min_size 的文件先查缓存（键为 size + mtime_ns + path）
    - 命中则跳过计算
    - 未命中则计算后存入缓存
    """
    if cache is None:
        # 无缓存，走原逻辑
        workers = _resolve_workers(num_workers, len(file_paths))
        if workers <= 1:
            return [_calculate_md5(p) for p in file_paths]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_calculate_md5, file_paths))

    # ── 有缓存：逐个处理，小于 cache_min_size 的直接算 ──
    results: list[bytes] = [b""] * len(file_paths)
    to_compute: list[tuple[int, str]] = []  # (index, path)

    # 构建缓存查询键：(size, mtime_ns)
    stat_infos: list[tuple[int, int] | None] = []
    for path in file_paths:
        try:
            st = os.stat(path)
            stat_infos.append((st.st_size, st.st_mtime_ns))
        except OSError:
            stat_infos.append(None)

    # 批量查询缓存
    query_items: list[tuple[int, int, str]] = []
    query_indices: list[int] = []
    for i, (path, stat_info) in enumerate(zip(file_paths, stat_infos)):
        if stat_info is None or stat_info[0] < cache_min_size:
            to_compute.append((i, path))
        else:
            query_items.append((stat_info[0], stat_info[1], path))
            query_indices.append(i)

    cached = cache.get_batch(query_items)

    for query_idx, key in enumerate(query_items):
        real_idx = query_indices[query_idx]
        if key in cached:
            results[real_idx] = bytes.fromhex(cached[key])
        else:
            to_compute.append((real_idx, file_paths[real_idx]))

    # 对未命中的文件计算 MD5
    if to_compute:
        workers = _resolve_workers(num_workers, len(to_compute))
        paths_to_compute = [p for _, p in to_compute]

        if workers <= 1:
            computed_hashes = [_calculate_md5(p) for p in paths_to_compute]
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=workers) as pool:
                computed_hashes = list(pool.map(_calculate_md5, paths_to_compute))

        to_store: list[tuple[int, int, str, str]] = []

        for (idx, path), hash_bytes in zip(to_compute, computed_hashes):
            results[idx] = hash_bytes

            # 只缓存 >= cache_min_size 的文件
            stat_info = stat_infos[idx]
            if stat_info is not None and stat_info[0] >= cache_min_size:
                md5_hex = hash_bytes.hex()
                to_store.append((stat_info[0], stat_info[1], path, md5_hex))

        if to_store:
            cache.set_batch(to_store)

    return results


def _read_prefix(file_path: str, file_size: int) -> bytes | None:
    """使用低级文件描述符读取文件前 PREFIX_SIZE 字节。读取失败返回 None。"""
    read_size = min(_PREFIX_SIZE, file_size)
    try:
        fd = os.open(file_path, _OPEN_FLAGS)
    except OSError:
        return None
    try:
        return os.read(fd, read_size)
    finally:
        os.close(fd)


def _read_prefix_batch(file_paths: list[str], file_size: int, num_workers: int) -> list[bytes | None]:
    """批量读取文件前缀，支持并行。读取失败的文件对应位置返回 None。"""
    if num_workers <= 1:
        return [_read_prefix(p, file_size) for p in file_paths]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        return list(pool.map(lambda p: _read_prefix(p, file_size), file_paths))


def _group_files_by_size(
    root_dir: Path,
    recursive: bool,
    exclude_dirs: Collection[str] | None = None,
    include_extensions: Collection[str] | None = None,
    exclude_extensions: Collection[str] | None = None,
    min_size: int = 1,
    max_size: int | None = None,
    follow_symlinks: bool = False,
) -> tuple[dict[int, list[str]], int]:
    """
    扫描目录并将文件按大小分组。

    Returns:
        (size_to_files, total_files) — size_to_files 为 {大小: [路径列表]}，
        total_files 为经筛选后的总文件数。
    """
    size_to_files: dict[int, list[str]] = {}
    total_files = 0

    for file_path, file_size in _iter_files(
        root_dir,
        recursive,
        exclude_dirs,
        include_extensions,
        exclude_extensions,
        min_size,
        max_size,
        follow_symlinks,
    ):
        total_files += 1
        size_to_files.setdefault(file_size, []).append(file_path)

    return size_to_files, total_files


def _get_file_metadata(file_path: str, file_size: int) -> FileInfo:
    """获取文件元数据（路径、大小、三个时间戳），跨平台兼容。

    使用纳秒整数时间戳：
    - accessed: st_atime_ns（最后访问）
    - modified: st_mtime_ns（最后修改）
    - changed:  st_ctime_ns（元数据更改；Windows 上为创建时间）
    """
    st = os.stat(file_path)
    return FileInfo(
        path=file_path,
        size=file_size,
        accessed=st.st_atime_ns,
        modified=st.st_mtime_ns,
        changed=st.st_ctime_ns,
    )


def _sort_key_for_strategy(sort_strategy: str):
    """返回用于 min() 的排序键函数，根据策略选出应保留的文件。

    使用 (时间戳, 路径) 元组作为排序键：时间戳相同时以路径打破平局，
    确保不同策略能选出不同的保留文件。
    """
    attr = _SORT_KEY_ATTR.get(sort_strategy, "changed")
    reverse = _SORT_REVERSE.get(sort_strategy, False)
    if reverse:
        return lambda f: (-getattr(f, attr), f.path)
    return lambda f: (getattr(f, attr), f.path)


def _mark_keep(files: list[FileInfo], sort_strategy: str) -> None:
    """在原文件列表上直接标记 keep=True（仅标记一个，其余为 False）。"""
    if not files:
        return
    if len(files) == 1:
        files[0].keep = True
        return

    key_fn = _sort_key_for_strategy(sort_strategy)
    keep_file = min(files, key=key_fn)
    for f in files:
        f.keep = f is keep_file


def _find_prefix_duplicates(
    file_paths: list[str],
    file_size: int,
    num_workers: int,
    sort_strategy: str = "oldest_changed",
    cache: MD5Cache | None = None,
    cache_min_size: int = 5 * 1024 * 1024,
) -> list[list[FileInfo]]:
    """
    对前缀相同的一组文件计算完整 MD5，找出重复组。

    组内文件按路径排序，并标记 keep 标记。

    Returns:
        重复组列表，每组为 FileInfo 列表（≥2 个文件）。
    """
    md5_to_paths: dict[bytes, list[str]] = {}

    for file_path, file_hash in zip(
        file_paths,
        _compute_md5_batch(file_paths, num_workers, cache=cache, cache_min_size=cache_min_size),
        strict=False,
    ):
        md5_to_paths.setdefault(file_hash, []).append(file_path)

    groups: list[list[FileInfo]] = []
    for paths in md5_to_paths.values():
        if len(paths) < 2:
            continue
        file_infos = [_get_file_metadata(p, file_size) for p in sorted(paths)]
        _mark_keep(file_infos, sort_strategy)
        groups.append(file_infos)

    return groups


def _process_size_group_for_scan(
    file_size: int,
    same_size_files: list[str],
    num_workers: int,
    sort_strategy: str = "oldest_changed",
    cache: MD5Cache | None = None,
    cache_min_size: int = 5 * 1024 * 1024,
) -> list[DuplicateGroup]:
    """处理单个 size 组，返回该组内的 DuplicateGroup 列表。"""
    prefix_to_files: dict[bytes, list[str]] = {}

    for file_path, prefix in zip(
        same_size_files,
        _read_prefix_batch(same_size_files, file_size, num_workers),
        strict=False,
    ):
        if prefix is None:  # 读取失败，跳过该文件
            continue
        prefix_to_files.setdefault(prefix, []).append(file_path)

    group_results: list[DuplicateGroup] = []
    for same_prefix_files in prefix_to_files.values():
        if len(same_prefix_files) < 2:
            continue

        dup_groups = _find_prefix_duplicates(
            same_prefix_files,
            file_size,
            num_workers,
            sort_strategy,
            cache=cache,
            cache_min_size=cache_min_size,
        )
        for files in dup_groups:
            group_results.append(
                DuplicateGroup(
                    size=file_size,
                    files=files,
                )
            )

    return group_results


# ──────────────────────────── 公开类 ────────────────────────────


class FileDedup:
    """文件去重工具类，提供扫描查找和分步删除两个阶段的操作。"""

    @classmethod
    def find_duplicates(
        cls,
        folder_path: str | Path,
        recursive: bool = True,
        exclude_dirs: Collection[str] | None = None,
        include_extensions: Collection[str] | None = None,
        exclude_extensions: Collection[str] | None = None,
        min_size: int = 1,
        max_size: int | None = None,
        follow_symlinks: bool = False,
        num_workers: int = 0,
        max_results: int = 1000,
        sort_strategy: str = "oldest_changed",
        use_cache: bool = False,
        cache_min_size: int = 5 * 1024 * 1024,
    ) -> ScanResult:
        """
        扫描文件夹，查找重复文件并按大小分组返回（只读操作，不修改任何文件）。

        返回每个文件的完整元数据，并根据 sort_strategy 在每组的文件上标记 keep 字段。
        前端拿到数据后可直接渲染，无需二次加工。

        处理流程：
        1. 扫描时按条件过滤文件。
        2. 按文件大小分组，只有大小相同的文件（≥2 个）才继续。
        3. 同大小文件读取前 256 字节分组，排除内容明显不同的文件。
        4. 前缀也相同的文件计算完整 MD5 确认是否重复。

        Args:
            folder_path: 要扫描的目标文件夹路径。
            recursive: 是否递归扫描子目录，默认为 True。
            exclude_dirs: 要跳过的目录名集合，默认为常见隐藏/缓存目录。
            include_extensions: 仅处理指定扩展名的文件，为 None 时处理所有文件。
            exclude_extensions: 跳过指定扩展名的文件，为 None 时不跳过。
            min_size: 最小文件字节数，小于此值的文件跳过，默认为 1（跳过空文件）。
            max_size: 最大文件字节数，大于此值的文件跳过，为 None 时不限制。
            follow_symlinks: 是否跟踪符号链接，默认为 False。
            num_workers: 工作线程数。0=自动，1=串行，>1=指定线程数并行。
            max_results: 返回的重复文件数量上限，默认为 1000。超过此数量后
                         停止收集，ScanResult.has_more 会标记为 True。
            sort_strategy: 保留策略，可选值：oldest_changed / newest_changed /
                           oldest_modified / newest_modified /
                           oldest_accessed / newest_accessed。默认 oldest_changed。
            use_cache: 是否启用 MD5 缓存（默认 False）。启用后对 >= cache_min_size
                       的文件缓存 MD5，下次扫描相同文件可直接命中。
            cache_min_size: 仅缓存 >= 此字节数的文件，默认 5 MB。

        Returns:
            ScanResult 实例。
        """
        start_time = time.perf_counter()

        root_dir = _normalize_root_dir(folder_path)

        if exclude_dirs is None:
            exclude_dirs = _DEFAULT_EXCLUDE_DIRS

        # 初始化缓存（仅在 use_cache=True 时）
        cache: MD5Cache | None = None
        if use_cache:
            cache = MD5Cache()

        size_to_files, total_files = _group_files_by_size(
            root_dir,
            recursive,
            exclude_dirs,
            include_extensions,
            exclude_extensions,
            min_size,
            max_size,
            follow_symlinks,
        )

        groups: list[DuplicateGroup] = []
        size_groups = [(size, files) for size, files in size_to_files.items() if len(files) >= 2]

        if num_workers != 1 and len(size_groups) > 1:
            from concurrent.futures import ThreadPoolExecutor

            workers = _resolve_workers(num_workers, len(size_groups))

            # 顶层并行处理 size_groups，内部强制串行（避免嵌套线程池）
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(
                        _process_size_group_for_scan,
                        size,
                        files,
                        1,
                        sort_strategy,
                        cache,
                        cache_min_size,
                    )
                    for size, files in size_groups
                ]
                for future in futures:
                    group_results = future.result()
                    groups.extend(group_results)
        else:
            for file_size, same_size_files in size_groups:
                group_results = _process_size_group_for_scan(
                    file_size,
                    same_size_files,
                    num_workers,
                    sort_strategy,
                    cache,
                    cache_min_size,
                )
                groups.extend(group_results)

        # 按文件大小降序排列
        groups.sort(key=lambda g: g.size, reverse=True)

        # 按 max_results 截断：优先保留大文件的重复组，最后一组支持部分截断
        has_more = False
        if max_results > 0:
            running_count = 0
            truncated: list[DuplicateGroup] = []
            for g in groups:
                if running_count + g.dup_count <= max_results:
                    truncated.append(g)
                    running_count += g.dup_count
                else:
                    # 部分截断：保留该组的前 N+1 个文件（N 个可删 + 1 个保留）
                    remaining = max_results - running_count
                    if remaining > 0:
                        partial_files = g.files[: remaining + 1]
                        # 重新标记 keep（因为截断后原 keep 文件可能不在内）
                        _mark_keep(partial_files, sort_strategy)
                        truncated.append(
                            DuplicateGroup(
                                size=g.size,
                                files=partial_files,
                            )
                        )
                    has_more = True
                    break
            groups = truncated

        elapsed = time.perf_counter() - start_time

        # 收集缓存统计并关闭缓存连接
        cache_stats = cache.stats() if cache else None
        if cache:
            cache.close()

        return ScanResult(
            dir=str(root_dir),
            total=total_files,
            groups=groups,
            time_s=round(elapsed, 4),
            has_more=has_more,
            cache_stats=cache_stats,
        )

    @classmethod
    def remove_duplicates(
        cls,
        file_paths: Collection[str | Path],
        send_to_trash: bool = False,
    ) -> RemoveResult:
        """
        根据路径列表删除文件（配合 find_duplicates 的结果使用）。

        两种处理模式：
        - send_to_trash=True：将文件移入系统回收站（需要安装 send2trash）。
        - 默认：直接永久删除文件。

        Args:
            file_paths: 要删除的文件路径列表。
            send_to_trash: 是否移入系统回收站而非永久删除，默认为 False。

        Returns:
            RemoveResult 实例。
        """
        start_time = time.perf_counter()

        removed: list[str] = []
        errors: list[RemoveError] = []
        reclaimed_bytes = 0

        if send_to_trash:
            from send2trash import send2trash

        for file_path in file_paths:
            file_path_str = os.fspath(file_path)
            try:
                file_size = os.path.getsize(file_path_str)

                if send_to_trash:
                    send2trash(file_path_str)
                else:
                    os.remove(file_path_str)

                removed.append(file_path_str)
                reclaimed_bytes += file_size
            except OSError as e:
                errors.append(RemoveError(file=file_path_str, error=str(e)))

        elapsed = time.perf_counter() - start_time

        return RemoveResult(
            removed=removed,
            errors=errors,
            reclaimed_bytes=reclaimed_bytes,
            time_s=round(elapsed, 4),
        )

    @classmethod
    def update_keep_marks(
        cls,
        groups: list[DuplicateGroup],
        sort_strategy: str,
    ) -> dict[str, bool]:
        """
        根据新策略重新标记每组的保留文件（无需重新扫描）。

        直接修改传入的 groups 中文件的 keep 字段，并返回
        {path: keep} 映射供前端使用。

        Args:
            groups: 缓存的扫描结果中的重复组列表。
            sort_strategy: 保留策略。

        Returns:
            {path: keep} 映射。
        """
        keep_map: dict[str, bool] = {}
        for g in groups:
            _mark_keep(g.files, sort_strategy)
            for f in g.files:
                keep_map[f.path] = f.keep
        return keep_map

    @classmethod
    def auto_clean(
        cls,
        folder_path: str | Path,
        *,
        recursive: bool = True,
        exclude_dirs: Collection[str] | None = None,
        include_extensions: Collection[str] | None = None,
        exclude_extensions: Collection[str] | None = None,
        min_size: int = 1,
        max_size: int | None = None,
        follow_symlinks: bool = False,
        num_workers: int = 0,
        sort_strategy: str = "oldest_changed",
        send_to_trash: bool = True,
        use_cache: bool = False,
        cache_min_size: int = 5 * 1024 * 1024,
    ) -> AutoCleanResult:
        """一键清除：扫描重复文件并自动删除所有非保留文件。

        组合 find_duplicates + remove_duplicates，无需用户交互确认。

        Args:
            send_to_trash: 是否移入回收站，默认 True（更安全）。
            其余参数同 find_duplicates。

        Returns:
            AutoCleanResult 实例。
        """
        start_time = time.perf_counter()

        # 1. 扫描
        scan_result = cls.find_duplicates(
            folder_path=folder_path,
            recursive=recursive,
            exclude_dirs=exclude_dirs,
            include_extensions=include_extensions,
            exclude_extensions=exclude_extensions,
            min_size=min_size,
            max_size=max_size,
            follow_symlinks=follow_symlinks,
            num_workers=num_workers,
            sort_strategy=sort_strategy,
            use_cache=use_cache,
            cache_min_size=cache_min_size,
        )

        # 2. 收集所有非保留文件
        dup_paths: list[str] = []
        for g in scan_result.groups:
            for f in g.files:
                if not f.keep:
                    dup_paths.append(f.path)

        # 3. 删除
        if dup_paths:
            remove_result = cls.remove_duplicates(dup_paths, send_to_trash=send_to_trash)
        else:
            remove_result = RemoveResult(removed=[], errors=[], reclaimed_bytes=0, time_s=0)

        elapsed = time.perf_counter() - start_time

        return AutoCleanResult(
            total_scanned=scan_result.total,
            dup_groups=scan_result.dup_groups,
            dup_files=scan_result.dups,
            removed=remove_result.removed,
            errors=remove_result.errors,
            reclaimed_bytes=remove_result.reclaimed_bytes,
            time_s=round(elapsed, 4),
        )
