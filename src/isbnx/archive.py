"""压缩包（zip/rar/uvz）ISBN 提取模块。

数据来源优先级（按速度从高到低）:

1. **meta.xml** — 压缩包内的 XML 元数据文件，含 ``<ssid>`` / ``<isbn>`` 字段。
   纯文本解析，最快（~10-20ms）。编码兼容 UTF-8 / GB18030。
2. **bookinfo.dat** — 超星 PDG 专用 INI 配置文件，含 ISBN / SSID。
   纯文本解析，较快（~5-10ms）。编码兼容 GB18030 / UTF-8。
3. **leg001.pdg** — 版权页图片（通常是封面页/书名页），
   解码后 ONNX 检测 + OCR 提取。中等速度（~200-500ms）。
4. **兜底 PDG** — 前 N 个 PDG 文件（由 ``pdg_fallback_count`` 控制），
   逐个解码 + ONNX + OCR。最慢（N × 200-500ms）。

合并策略: meta.xml 和 bookinfo.dat 的结果会**合并**（互不覆盖，
前面的来源优先级更高）。只要合并后 ISBN 或 SSID 任一有效即返回，
不再走更慢的图片路径。
"""

from __future__ import annotations

import ctypes
import importlib.resources as resources
import io
import os
import re
import sys
import tempfile
import time
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from PIL import Image

from isbnx.config import settings
from isbnx.detector import Detector, get_detector
from isbnx.models import BookInfo, ExtractResult, Locate, Meta
from isbnx.utils.filename import extract_from_filename

# ── 图片文件签名 ──
_JPEG_HEADER = b"\xff\xd8\xff"
_PNG_HEADER = b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a"

# ── 安全限制 ──
_MAX_BOOKINFO_SIZE = 1 * 1024 * 1024
_MAX_META_XML_SIZE = 256 * 1024
_MAX_PDG_SIZE = 50 * 1024 * 1024

# ── bookinfo.dat 字段映射（仅关注 ISBN） ──
_KEY_MAP: dict[str, str] = {
    "isbn号": "isbn",
    "isbn13": "isbn",
    "isbn10": "isbn",
    "isbn": "isbn",
    "ss号": "ssid",
    "ssid号": "ssid",
    "ssid": "ssid",
    "ss": "ssid",
}


# ═══════════════════════════════════════════════════════════
#  文件级工具函数
# ═══════════════════════════════════════════════════════════


def _get_names(arc) -> set[str]:
    """获取压缩包内所有文件路径的集合。"""
    return set(arc.namelist())


def _count_pdg(names: set[str]) -> int:
    """统计压缩包内 *.pdg 文件数量。"""
    return sum(1 for n in names if n.lower().endswith(".pdg"))


def _list_pdg(names: set[str]) -> list[str]:
    """列出压缩包内所有 *.pdg 文件路径（按文件名排序）。"""
    return sorted(n for n in names if n.lower().endswith(".pdg"))


# ── bookinfo.dat 解析 ──


def _decode_bookinfo(raw: bytes) -> tuple[str, str]:
    """解码 bookinfo.dat，返回 (文本, 编码)。"""
    try:
        text = raw.decode("gb18030")
        if "\ufffd" in text:
            text = raw.decode("utf-8", errors="replace")
            return text, "utf-8"
        return text, "gb18030"
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        return text, "utf-8"


def _parse_bookinfo(text: str) -> dict[str, str | None]:
    """从 bookinfo.dat 文本中提取 ISBN 和 SSID。"""
    result: dict[str, str | None] = {"isbn": None, "ssid": None}
    section: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section is None:
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue

        mapped = _KEY_MAP.get(key)
        if mapped is None:
            continue
        if result[mapped] is not None:
            continue  # 已有值则不覆盖

        if mapped == "isbn":
            cleaned = value.replace("-", "").replace(" ", "").upper()
            if len(cleaned) == 10:
                result["isbn"] = cleaned
            elif len(cleaned) == 13 and cleaned.startswith(("978", "979")):
                result["isbn"] = cleaned
        else:
            result[mapped] = value
    return result


# ── PDG 文件检测与解码 ──


def _is_image_file(data: bytes) -> bool:
    """检查字节数据是否为常见图片格式（jpg/png）。"""
    return data.startswith(_JPEG_HEADER) or data.startswith(_PNG_HEADER)


def _pdg_to_image(data: bytes) -> Image.Image | None:
    """将 PDG 字节数据解码为 PIL Image。

    流程:
      1. 检查文件头是否为常见图片格式（jpg/png）
      2. 是 → 直接用 PIL 读取（失败则继续）
      3. 否 → PdgView.dll 解码
    """
    if _is_image_file(data):
        try:
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            pass  # 不要直接返回 None，继续尝试 PdgView.dll

    # PdgView.dll 解码
    try:
        img = _pdg_decode_with_dll(data)
        if img is not None:
            return img
    except Exception:
        pass

    # 最后兜底
    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None


def _parse_pdg_header(data: bytes) -> tuple[int, int, int] | None:
    """解析 PDG 文件头，返回 (width, height, pdg_type) 或 None。"""
    if len(data) < 140:
        return None
    pdg_type = data[15]
    if pdg_type == 0xFF or pdg_type == 0x10:
        return None
    x_pix = int.from_bytes(data[16:18], "little", signed=False)
    y_pix = int.from_bytes(data[18:20], "little", signed=False)
    if pdg_type in (0xAA, 0xAC):
        x_pix, y_pix = 1120, 1568
    elif pdg_type == 0xAB:
        x_pix, y_pix = y_pix, x_pix
    return x_pix, y_pix, pdg_type


def _pdg_decode_with_dll(data: bytes) -> Image.Image | None:
    """使用 PdgView.dll 解码 PDG 文件（仅 Windows 平台支持）。"""
    if sys.platform != "win32":
        return None

    dll_path = resources.files(__package__) / settings.archive.pdgview_path
    if not dll_path.is_file():
        return None

    dll = ctypes.cdll.LoadLibrary(str(dll_path))
    dll.pdgInit()

    header = _parse_pdg_header(data)
    if header is None:
        return None
    x_pix, y_pix, _ = header

    fd, tmp_path = tempfile.mkstemp(suffix=".pdg")
    decoded_ok = False
    img_buffer_ptr = ctypes.c_void_p()
    try:
        os.write(fd, data)
        os.close(fd)

        size = ctypes.c_int()
        imgtype = ctypes.c_int()
        ret = dll.pdgDecode(
            ctypes.c_char_p(tmp_path.encode("utf-8") + b"\0"),
            ctypes.c_int(x_pix),
            ctypes.c_int(y_pix),
            ctypes.byref(img_buffer_ptr),
            ctypes.byref(size),
            ctypes.byref(imgtype),
        )
        if ret != 0 or not img_buffer_ptr:
            return None
        decoded_ok = True

        buf = (ctypes.c_byte * size.value).from_address(img_buffer_ptr.value)  # type: ignore[arg-type]
        img = Image.open(io.BytesIO(bytes(buf))).convert("RGB")
        return img
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        if decoded_ok:
            try:
                dll.pdgFreeBuffer(img_buffer_ptr)
            except Exception:
                pass


def _extract_result_from_image(
    data: bytes,
    detector,
    *,
    source: str,
    locate_page: int,
    locate_method: Literal["leg001", "cov", "pdg"],
) -> ExtractResult | None:
    """将 PDG 字节解码为图片后运行 ONNX 检测 + OCR，返回完整提取结果。"""
    img = _pdg_to_image(data)
    if img is None:
        return None
    det = detector or get_detector()
    result = det.process(img, source=source, source_type="archive")
    if result.locate is not None:
        result.locate.page = locate_page
        result.locate.method = locate_method
        result.locate.extraction = "ocr"
    else:
        result.locate = Locate(page=locate_page, method=locate_method, extraction="ocr")
    result.meta = Meta(source=source, source_type="archive")
    return result


# ═══════════════════════════════════════════════════════════
#  压缩包抽象层（统一 zip / rar / uvz 接口）
# ═══════════════════════════════════════════════════════════


class _ArchiveReader(ABC):
    """压缩包读取器抽象基类。"""

    @abstractmethod
    def namelist(self) -> list[str]: ...

    @abstractmethod
    def read(self, name: str) -> bytes: ...

    @abstractmethod
    def getinfo(self, name: str): ...

    @abstractmethod
    def is_encrypted(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _ZipReader(_ArchiveReader):
    """ZIP / UVZ 读取器。"""

    def __init__(self, path: str) -> None:
        self._zf = zipfile.ZipFile(path, "r")

    def namelist(self) -> list[str]:
        return self._zf.namelist()

    def read(self, name: str) -> bytes:
        return self._zf.read(name)

    def getinfo(self, name: str):
        return self._zf.getinfo(name)

    def is_encrypted(self) -> bool:
        for info in self._zf.infolist():
            if info.flag_bits & 0x1:
                return True
        return False

    def close(self) -> None:
        self._zf.close()


class _RarReader(_ArchiveReader):
    """RAR 读取器。"""

    def __init__(self, path: str) -> None:
        import rarfile  # noqa: PLC0415

        self._rf = rarfile.RarFile(path)

    def namelist(self) -> list[str]:
        return self._rf.namelist()

    def read(self, name: str) -> bytes:
        return self._rf.read(name)

    def getinfo(self, name: str):
        return self._rf.getinfo(name)

    def is_encrypted(self) -> bool:
        return any(getattr(info, "encrypted", False) for info in self._rf.infolist())

    def close(self) -> None:
        self._rf.close()


class _SevenZipReader(_ArchiveReader):
    """7z 读取器。

    注意：py7zr 没有提供直接读取文件内容的接口，
    因此 ``read()`` 会将目标文件解压到临时目录后再读入内存。

    另外，py7zr 的同一个 ``SevenZipFile`` 实例上多次调用 ``extract()``
    会出现 CrcError（内部解压状态污染），所以每次 ``read()`` 都会
    打开一个新的 ``SevenZipFile`` 实例。
    """

    def __init__(self, path: str) -> None:
        import tempfile  # noqa: PLC0415

        import py7zr  # noqa: PLC0415

        self._path = path
        self._sz = py7zr.SevenZipFile(path, mode="r")
        self._tmpdir = tempfile.mkdtemp(prefix="isbnx_7z_")

    def namelist(self) -> list[str]:
        return self._sz.getnames()

    def read(self, name: str) -> bytes:
        from pathlib import Path  # noqa: PLC0415

        import py7zr  # noqa: PLC0415

        # 每次 read 都开新实例，避免 py7zr 多次 extract 的 CrcError
        with py7zr.SevenZipFile(self._path, mode="r") as sz:
            sz.extract(path=self._tmpdir, targets=[name])
        target = Path(self._tmpdir) / name
        if not target.exists():
            raise KeyError(name)
        return target.read_bytes()

    def getinfo(self, name: str):
        return self._sz.getinfo(name)

    def is_encrypted(self) -> bool:
        try:
            return bool(self._sz.needs_password())
        except Exception:
            return False

    def close(self) -> None:
        import shutil  # noqa: PLC0415

        try:
            self._sz.close()
        finally:
            shutil.rmtree(self._tmpdir, ignore_errors=True)


def _open_archive(path: Path) -> _ArchiveReader:
    """根据文件扩展名打开压缩包。"""
    ext = path.suffix.lower()
    if ext in (".zip", ".uvz"):
        return _ZipReader(str(path))
    if ext == ".rar":
        return _RarReader(str(path))
    if ext == ".7z":
        return _SevenZipReader(str(path))
    raise ValueError(f"不支持的压缩包格式: {ext}（支持 zip/rar/7z/uvz）")


def _get_info_ignore_case(arc: _ArchiveReader, names: set[str], target: str):
    """按文件名（忽略大小写、忽略目录层级）查找文件信息。"""
    target_lower = target.lower()
    for name in names:
        if Path(name).name.lower() == target_lower:
            try:
                return arc.getinfo(name)
            except Exception:
                return None
    return None


def _read_file_ignore_case(arc: _ArchiveReader, names: set[str], target: str) -> bytes | None:
    """按文件名（忽略大小写、忽略目录层级）读取文件。"""
    target_lower = target.lower()
    for name in names:
        if Path(name).name.lower() == target_lower:
            try:
                return arc.read(name)
            except Exception:
                return None
    return None


# ── meta.xml 解析 ──


def _parse_meta_xml(raw: bytes) -> dict[str, str | None]:
    """解析压缩包内的 meta.xml，提取 ISBN 和 SSID。

    XML 格式示例::

        <meta>
        <ssid>96391214</ssid>
        <title>李鸿藻书札</title>
        <isbn>9787550845091</isbn>
        <creator>陆德富，谢亚衡编</creator>
        <publisher>西泠印社出版社</publisher>
        <date>2024.06</date>
        </meta>

    Returns:
        {"isbn": ..., "ssid": ...}，未找到的字段为 None。
    """
    import xml.etree.ElementTree as ET

    def _parse(raw: bytes) -> ET.Element | None:
        """尝试解析 XML 字节数据，支持编码兜底。"""
        # 1. 直接解析（适用于 UTF-8 / ASCII）
        try:
            return ET.fromstring(raw)
        except (ET.ParseError, ValueError):
            pass
        # 2. 编码兜底：先按 GB18030 解码为文本，去除 XML 声明中的编码信息，
        #    再重新编码为 UTF-8 后解析。解决 GB18030 编码或声明与实际不符的问题。
        try:
            text = raw.decode("gb18030")
            # 移除/替换 XML 声明中的 encoding，避免声明与真实编码冲突
            text = re.sub(
                r'<\?xml\s+[^>]*encoding\s*=\s*["\'][^"\']+["\'][^>]*\?>',
                '<?xml version="1.0" encoding="UTF-8"?>',
                text,
            )
            return ET.fromstring(text.encode("utf-8"))
        except (ET.ParseError, UnicodeDecodeError, UnicodeError, ValueError):
            return None

    result: dict[str, str | None] = {"isbn": None, "ssid": None}
    root = _parse(raw)
    if root is None:
        return result

    for field in ("isbn", "ssid"):
        elem = root.find(field)
        if elem is not None and elem.text:
            value = elem.text.strip()
            if field == "isbn":
                cleaned = value.replace("-", "").replace(" ", "").upper()
                if len(cleaned) == 10:
                    result["isbn"] = cleaned
                elif len(cleaned) == 13 and cleaned.startswith(("978", "979")):
                    result["isbn"] = cleaned
            else:
                result[field] = value
    return result


def _merge_metadata(*sources: dict[str, str | None]) -> dict[str, str | None]:
    """合并多个来源的元数据，前面的来源优先级更高（已有值不覆写）。"""
    merged: dict[str, str | None] = {}
    for src in sources:
        for key in ("isbn", "ssid"):
            if key not in merged and src.get(key):
                merged[key] = src[key]
    return merged


# ═══════════════════════════════════════════════════════════
#  ArchiveExtractor
# ═══════════════════════════════════════════════════════════


class ArchiveExtractor:
    """压缩包（zip/rar/uvz）ISBN 提取器。"""

    @classmethod
    def extract(
        cls,
        archive_path: str | Path,
        detector: Detector | None = None,
        *,
        filename: bool = False,
    ) -> ExtractResult:
        """从压缩包（zip/rar/uvz）中提取 ISBN。

        Args:
            archive_path: 压缩包文件路径。
            detector: 外部传入的 Detector 实例，为 None 时使用全局单例。
            filename: 是否优先从文件名中提取 ISBN。

        Returns:
            ExtractResult — 包含 ISBN、源信息等。
        """
        t0 = time.perf_counter()
        archive_path = Path(archive_path)

        if filename:
            info = extract_from_filename(archive_path)
            if info:
                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(archive_path), source_type="archive"),
                    elapsed=0.0,
                )

        try:
            with _open_archive(archive_path) as arc:
                names = _get_names(arc)

                # ── 步骤 0: 密码检查 ──
                if arc.is_encrypted():
                    return ExtractResult(
                        bookinfo=BookInfo(),
                        meta=Meta(source=str(archive_path), source_type="archive"),
                        error="压缩包有密码保护",
                        elapsed=time.perf_counter() - t0,
                    )

                # ── 步骤 1: PDG 数量检查 ──
                pdg_count = _count_pdg(names)
                if pdg_count < settings.archive.pdg_min_count:
                    return ExtractResult(
                        bookinfo=BookInfo(),
                        meta=Meta(source=str(archive_path), source_type="archive"),
                        error=f"PDG 数量不足: {pdg_count} < {settings.archive.pdg_min_count}",
                        elapsed=time.perf_counter() - t0,
                    )

                # ── 步骤 2: 元数据文件解析（meta.xml + bookinfo.dat）──
                meta_parsed: dict[str, str | None] = {}
                bookinfo_parsed: dict[str, str | None] = {}
                encoding: str | None = None
                locate_method: str | None = None

                # 2a: meta.xml
                mx_info = _get_info_ignore_case(arc, names, "meta.xml")
                if mx_info is not None and getattr(mx_info, "file_size", 0) <= _MAX_META_XML_SIZE:
                    raw = _read_file_ignore_case(arc, names, "meta.xml")
                    if raw:
                        meta_parsed = _parse_meta_xml(raw)
                        if meta_parsed.get("isbn") or meta_parsed.get("ssid"):
                            locate_method = "meta"

                # 2b: bookinfo.dat
                bi_info = _get_info_ignore_case(arc, names, "bookinfo.dat")
                if bi_info is not None and getattr(bi_info, "file_size", 0) <= _MAX_BOOKINFO_SIZE:
                    raw = _read_file_ignore_case(arc, names, "bookinfo.dat")
                    if raw:
                        text, enc = _decode_bookinfo(raw)
                        encoding = enc
                        bookinfo_parsed = _parse_bookinfo(text)
                        if bookinfo_parsed.get("isbn") or bookinfo_parsed.get("ssid"):
                            locate_method = "bookinfo"

                # 2c: 合并两路结果
                merged = _merge_metadata(meta_parsed, bookinfo_parsed)
                if merged.get("isbn") or merged.get("ssid"):
                    bookinfo = BookInfo(**merged)
                    if bookinfo.is_valid():
                        # 页码区分来源: -20=bookinfo.dat, -21=meta.xml
                        locate_page = -20 if locate_method == "bookinfo" else -21
                        return ExtractResult(
                            bookinfo=bookinfo,
                            meta=Meta(source=str(archive_path), source_type="archive", encoding=encoding),
                            locate=Locate(page=locate_page, method=locate_method or "meta", extraction="text"),
                            elapsed=time.perf_counter() - t0,
                        )

                # ── 步骤 3: leg/cov 开头的 PDG → 图片 → ONNX ──
                all_pdg = _list_pdg(names)
                leg_pdg: list[str] = []
                cov_pdg: list[str] = []
                for name in all_pdg:
                    stem = Path(name).stem.lower()
                    if stem.startswith("leg"):
                        leg_pdg.append(name)
                    elif stem.startswith("cov"):
                        cov_pdg.append(name)
                # leg 优先于 cov
                for pdg_name in leg_pdg + cov_pdg:
                    try:
                        info = arc.getinfo(pdg_name)
                        if getattr(info, "file_size", 0) > _MAX_PDG_SIZE:
                            continue
                        raw = arc.read(pdg_name)
                    except Exception:
                        continue
                    locate_method = "leg001" if Path(pdg_name).stem.lower().startswith("leg") else "cov"
                    result = _extract_result_from_image(
                        raw,
                        detector,
                        source=str(archive_path),
                        locate_page=-10,
                        locate_method=locate_method,
                    )
                    if result and result.success:
                        result.elapsed = time.perf_counter() - t0
                        return result

                # ── 步骤 4: 兜底 — 前 N 个 PDG ──
                pdg_files = [n for n in all_pdg if n not in set(leg_pdg + cov_pdg)]
                fallback_count = settings.archive.pdg_fallback_count
                for idx, pdg_name in enumerate(pdg_files[:fallback_count]):
                    try:
                        info = arc.getinfo(pdg_name)
                        if getattr(info, "file_size", 0) > _MAX_PDG_SIZE:
                            continue
                        raw_pdg = arc.read(pdg_name)
                    except Exception:
                        continue
                    result = _extract_result_from_image(
                        raw_pdg,
                        detector,
                        source=str(archive_path),
                        locate_page=idx + 1,
                        locate_method="pdg",
                    )
                    if result and result.success:
                        result.elapsed = time.perf_counter() - t0
                        return result

                # 所有步骤均失败
                return ExtractResult(
                    bookinfo=BookInfo(),
                    meta=Meta(source=str(archive_path), source_type="archive"),
                    error="未提取到 ISBN",
                    elapsed=time.perf_counter() - t0,
                )

        except Exception as e:
            return ExtractResult(
                bookinfo=BookInfo(),
                meta=Meta(source=str(archive_path), source_type="archive"),
                error=f"压缩包提取异常: {e}",
                elapsed=time.perf_counter() - t0,
            )
