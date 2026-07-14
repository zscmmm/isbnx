## isbnx 代码审查与改进计划

基于 dignified-python 编码标准，对 `src/isbnx/` 全部源码逐项审查后，按优先级从高到低整理出以下改进点。每个条目给出问题描述、涉及文件、以及建议的改法。

---

### P0 — 正确性与健壮性

#### 1. ISBN-10 硬编码只接受 "7" 开头（中国区）— ✅ 已处理

**文件：** `utils/isbn_utils.py` L71

**结论：** 设计意图明确 — 本项目仅面向中国大陆出版书籍。已补充注释说明组号含义及如何扩展支持外国书籍。

#### 2. `settings` 全局可变单例 — 线程安全问题 — ✅ 已处理

**文件：** `config.py`, `isbnx.py`, `detector.py`, `pdf.py`, `archive.py`, `epub.py`

`ISBNX.__init__` 的 `_apply_config()` 已移除，不再写全局 `settings`。改为：
- `ISBNX` 存储 `self.config`，创建 `Detector(self.config)` 实例级检测器
- `Detector.__init__` 接收 `config` 参数，所有方法读取 `self._config` 而非全局 `settings`
- `PdfExtractor.extract()` 和 `ArchiveExtractor.extract()` 接收 `config` 参数
- `EpubExtractor.extract()` 接收 `detector` 参数，图片扫描使用传入的检测器
- 全局 `get_detector()` 保留为回退工厂，不再被主路径调用

#### 3. `_ArchiveReader.getinfo()` 返回类型不明确 — ✅ 已处理

**文件：** `archive.py`

`getinfo()` 已重命名为 `get_file_size(name) -> int`，三个实现（Zip/Rar/7z）统一返回文件大小（字节），消除类型歧义。`_get_info_ignore_case` 同步重命名为 `_get_file_size_ignore_case`。

#### 4. `BookInfo.is_valid()` 耦合全局 `settings.strict` — ✅ 已处理

**文件：** `models.py`

`is_valid()` 签名改为 `is_valid(self, strict: int = 3) -> bool`，不再隐式读取全局 `settings`。调用方（`archive.py`、`models.py` 的 `success` 属性）必须显式传入 strict 值。

---

### P1 — LBYL 与异常处理（dignified-python 核心原则）

#### 5. 大量 `except Exception: pass` / `continue` — ✅ 已处理

**涉及文件：** `archive.py`, `pdf.py`, `pdf_type.py`, `epub.py`, `mobi.py`, `detector.py`

所有 `except Exception` 已缩窄为具体异常类型：
- `archive.py`: `(OSError, zipfile.BadZipFile, ValueError)`, `(KeyError, OSError)`, `(OSError, ValueError)` + `logger.debug`
- `pdf.py`: `(RuntimeError, ValueError, OSError)`, `(IndexError, RuntimeError, AttributeError)`
- `pdf_type.py`: `(RuntimeError, ValueError)`, `(ImportError, RuntimeError, ValueError)`
- `epub.py`: `(KeyError, OSError, zipfile.BadZipFile)`, `(KeyError, OSError)`
- `mobi.py`: 移除 `_parse_layout` 外层 try/except（LBYL 守卫已覆盖）；外层缩窄为 `(OSError, struct.error, ValueError)`

#### 6. 缺少 B904 异常链（`from e` / `from None`）— ✅ 已处理

**文件：** `__init__.py`

`raise AttributeError(msg) from None` 已添加。

#### 7. `archive.py` 外层 `except Exception` 过于宽泛 — ✅ 已处理

**文件：** `archive.py`

改为 `except (OSError, zipfile.BadZipFile, ValueError)`。

---

### P2 — 重复代码消除（DRY）

#### 8. EPUB / MOBI 模块与 isbn_utils 的 ISBN 扫描逻辑重复 — ✅ 已处理

**文件：** `epub.py`, `mobi.py`, `utils/isbn_utils.py`

已提取到 `isbn_utils.py` 的公共 API：
- `BYTE_GATE` — 字节级 ISBN 预过滤器
- `decode_bytes(data)` — 多编码解码（utf-8 → utf-16-le → gb18030 → big5）
- `extract_isbn(text)` — 从文本行中提取 ISBN（含 OCR 容错）
- `validate_and_format(raw)` — ISBN 清洗 + 校验 + 格式化
- `ISBN_KEYWORD` — OCR 容错正则（`1SBN/IS8N/I5BN`）

EPUB/MOBI 模块的 `_scan()` / `_clean()` / `_validate()` / `_decode()` / `_ok()` / `_fail()` 全部移除，改用共享 API。

#### 9. `_ok()` / `_fail()` 模式重复 — ✅ 已处理

**文件：** `models.py`

`ExtractResult.ok(source, source_type, isbn, t0)` 和 `ExtractResult.fail(source, source_type, error, t0)` 工厂方法已添加，EPUB/MOBI 统一使用。

#### 10. `cip_rules.py` 的 `_is_valid_isbn` 与 `isbn_utils.is_valid_isbn` 重复 — ✅ 已处理

**文件：** `cip_rules.py`

移除本地 `_is_valid_isbn`，改用 `from isbnx.utils.isbn_utils import is_valid_isbn as _is_valid_isbn`。4 处调用点添加 `candidate is not None and` 守卫以适配新签名。

---

### P3 — API 设计

#### 11. `Batch.__init__` 有 25+ 个参数 — ✅ 已处理

**文件：** `batch.py`

引入 `BatchConfig` dataclass，将所有非路径参数（25+ 个）分组封装：
- 扫描组: extensions, exclude_dirs, recursive, max_workers
- 重命名组: rename_mode, normalize_ext, keep_name, max_name_len
- 预检组: skip_isbn, skip_ssid
- PDF 组: pdf_front_start/pdf_front_end/pdf_back_start/pdf_back_end
- 去重组: deduplicate, dedup_read_size
- 输出组: keep_tree, dry_run, report_path, remove_empty_dirs, max_entries
- 显示组: quiet, show_progress
- 回调/控制组: progress_callback, entries_callback, shutdown_event

`Batch.__init__` 保持相同签名（向后兼容），内部构建 `self._cfg = BatchConfig(...)` 并统一使用 `self._cfg.X`。参数校验移入 `BatchConfig.__post_init__`。

`BatchConfig` 已导出至 `isbnx.__all__`。

#### 12. `ArchiveExtractor.extract()` 的参数设计

**文件：** `archive.py`

已添加 `config` 关键字参数。其他 Extractor 同步添加。Detector 参数保持可选。

#### 13. `Detect` 和 `Locate` 的 `__repr__` 过于详细

**文件：** `models.py`

暂未修改，优先级较低。

---

### P4 — 模块设计与导入

#### 14. `pdf.py` 模块级副作用 — ✅ 已处理

**文件：** `pdf.py`

MuPDF 抑制代码包裹在 `@cache` 函数 `_suppress_mupdf_output()` 中，首次调用 `_open_pdf()` 时惰性执行，`import isbnx.pdf` 不再产生副作用。

#### 15. `__init__.py` 的 `__getattr__` 懒加载 — ✅ 已处理

**文件：** `__init__.py`

`_lazy_map` 提为模块级常量 `_LAZY_IMPORT_MAP`。

#### 16. `isbnx.py` 大量 inline import — ✅ 已处理

**文件：** `isbnx.py`

轻量导入（`BookInfo`, `ExtractResult`, `Meta`, `extract_from_filename`, `detect_file_kind`, `require_suffix`）提至模块顶部。重型导入（`PdfExtractor`, `EpubExtractor`, `MobiExtractor`, `ArchiveExtractor`, `load_image`, `_pdg_to_image`）保留 inline 以避免触发 onnxruntime/pymupdf/numpy 加载。

---

### P5 — 类型注解完善

#### 17. `_ArchiveReader.getinfo()` 缺少返回类型 — ✅ 已处理

（已在 P0-3 处理：重命名为 `get_file_size() -> int`）

#### 18. `detector.py` 多个方法缺少返回类型 — ✅ 已处理

**文件：** `detector.py`

已添加：`_preprocess() -> tuple`, `_run() -> "np.ndarray"`, `_pick_boxes() -> list[tuple]`, `_scale_box() -> tuple[int, int, int, int] | None`。

#### 19. `config.py` 的 `configure()` 类型宽松

**文件：** `config.py`

`**kwargs: Any` 暂未修改，需要引入 `TypedDict` 或 `Unpack[SettingsDict]`，优先级较低。

---

### P6 — 小改进

#### 20. `detect_pdf_type()` 将 "mixed" 归为 "scanned" — ✅ 已处理

**文件：** `pdf.py`

`PdfExtractor.extract()` 的文本搜索条件从 `pdf_type == "text_based"` 改为 `pdf_type in ("text_based", "mixed")`。mixed PDF 先走文本搜索（快路径），未命中再回退 ONNX 检测。

#### 21. `_ISBN_KEYWORD_PDF` 与 `_ISBN_KEYWORD` 重复定义 — ✅ 已处理

**文件：** `detector.py`, `pdf.py`, `utils/isbn_utils.py`

统一为 `isbn_utils.ISBN_KEYWORD`，detector.py 和 pdf.py 的本地副本已移除。

#### 22. `_SevenZipReader.__init__` 中 `import tempfile` 冗余 — ✅ 已处理

**文件：** `archive.py`

冗余的 inline `import tempfile` 已移除，使用模块顶部导入。

#### 23. `batch.py` 的 `_deduplicate()` 在非 dry-run 模式下直接 `fp.unlink()` 删除文件 — ✅ 已处理

**文件：** `batch.py`

`fp.unlink()` 替换为 `_trash_file(fp)` 方法，将重复文件安全移入 `source_dir/_trash/` 目录。同名文件自动追加计数器。`_trash` 已加入 `DEFAULT_EXCLUDE_DIRS` 避免重扫。

---

### 实施状态总结

| 批次 | 条目 | 状态 |
|------|------|------|
| **P0** | 1-4 | ✅ 全部完成 |
| **P1** | 5-7 | ✅ 全部完成 |
| **P2** | 8-10 | ✅ 全部完成 |
| **P3** | 11-12 | ✅ 已完成（13 暂缓）|
| **P4** | 14-16 | ✅ 全部完成 |
| **P5** | 17-18 | ✅ 已完成（19 暂缓）|
| **P6** | 20-23 | ✅ 全部完成 |

**已完成：21/23 项**，剩余 2 项（P3-13 `__repr__` 重构、P5-19 `configure()` 类型）优先级较低，暂不修改。
