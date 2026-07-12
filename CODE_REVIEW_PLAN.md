## isbnx 代码审查与改进计划

基于 dignified-python 编码标准，对 `src/isbnx/` 全部源码逐项审查后，按优先级从高到低整理出以下改进点。每个条目给出问题描述、涉及文件、以及建议的改法。

---

### P0 — 正确性与健壮性

#### 1. ISBN-10 硬编码只接受 "7" 开头（中国区）— ✅ 已处理

**文件：** `utils/isbn_utils.py` L71

**结论：** 设计意图明确 — 本项目仅面向中国大陆出版书籍。已补充注释说明组号含义及如何扩展支持外国书籍。

#### 2. `settings` 全局可变单例 — 线程安全问题

**文件：** `config.py` L127, `models.py` L17

`settings = Settings()` 是模块级可变对象，`configure()` 会直接修改它。`batch.py` 用 `ThreadPoolExecutor` 多线程并行提取，每个线程的 `ISBNX` 实例通过 `_apply_config()` 也会修改全局 `settings`。多线程同时写全局 settings 是 data race。

**建议：**
- `ISBNX.__init__` 接收 `Settings` 后不再写全局 settings，改为实例级配置透传
- `BookInfo.is_valid()` 不从 `settings` 读 strict，改为由调用方显式传入
- 各 Extractor 的 `extract()` 方法加 `config` 参数，不依赖全局 `settings`

#### 3. `_ArchiveReader.getinfo()` 返回类型不明确

**文件：** `archive.py` L282

```python
@abstractmethod
def getinfo(self, name: str): ...
```

三个实现返回不同类型（`zipfile.ZipInfo`、`rarfile.RarInfo`、`py7zr.FileInfo`），调用方靠 `getattr(info, "file_size", 0)` 做兼容。应在抽象层定义最小接口，或使用 Protocol 统一类型。

**建议：** 让 `getinfo()` 返回 `int`（file_size），因为当前调用方只用了 `file_size` 这一个属性。

#### 4. `BookInfo.is_valid()` 耦合全局 `settings.strict`

**文件：** `models.py` L220-236

```python
def is_valid(self, strict: int | None = None) -> bool:
    if strict is None:
        strict = settings.strict
```

数据模型依赖全局配置，违反关注点分离。`ExtractResult.success` 属性（L270）调用 `self.bookinfo.is_valid()` 时隐式读取全局 strict，导致同一个 ExtractResult 对象在 strict 改变后 `success` 值会变化，行为不可预测。

**建议：** `is_valid()` 移除 `strict=None` 回退，要求调用方始终显式传入。或在 `ExtractResult` 创建时就固化 success 值。

---

### P1 — LBYL 与异常处理（dignified-python 核心原则）

#### 5. 大量 `except Exception: pass` / `continue` — 静默吞异常

**涉及文件和行号：**
- `archive.py` L157, L165（`_pdg_to_image`）
- `archive.py` L233-234（`_pdg_decode_with_dll` cleanup）
- `archive.py` L387-388（`_SevenZipReader.is_encrypted`）
- `archive.py` L419（`_get_info_ignore_case`）
- `archive.py` L431（`_read_file_ignore_case`）
- `archive.py` L627, L650（ArchiveExtractor.extract PDG 读取循环）
- `pdf.py` L59-60（`_open_pdf` 格式错误）
- `pdf.py` L166（`_extract_text_from_page`）
- `pdf_type.py` L54-55（`detect_pdf_type`）
- `epub.py` L96-97, L133, L190
- `mobi.py` L80-81, L128-129, L179-180

根据 dignified-python 标准，异常只能在 error boundary、第三方 API 兼容、添加上下文重抛三种场景下使用。当前代码大量裸 `except Exception` 静默吞掉错误，违反核心原则。

**建议分两层处理：**
- **合理保留：** archive.py 的 PDG 读取循环、_get_info_ignore_case 等属于"第三方库 API 无 LBYL 替代"的场景，可保留但应缩窄到具体异常类型（如 `KeyError`, `zipfile.BadZipFile`），并加 `logger.debug` 记录
- **应修复：** `_pdg_to_image` 的两层 `except Exception: pass` 应改为先检查文件头再选择解码器（已经是 LBYL 了，但后续 fallback 又用 EAFP）；`pdf.py` 的 `_open_pdf` 应先用 `pdf_path.exists()` 检查（已做），然后用具体异常类型

#### 6. 缺少 B904 异常链（`from e` / `from None`）

**涉及文件：**
- `isbnx_rapiocr.py` L35: `raise ImportError(...) from None` — 这个是对的
- `__init__.py` L49: `raise AttributeError(msg)` — 缺少 `from None`
- `archive.py` L409: `raise ValueError(...)` — 在 `_open_archive` 中没有 except 上下文，OK
- `config.py` L153-154: `except (TypeError, ValueError) as e` 然后只 `logger.warning` — 这里 OK 但吞了异常

**建议：** 审查所有 `except ... : raise` 模式，确保有 `from e` 或 `from None`。

#### 7. `archive.py` 外层 `except Exception` 过于宽泛

**文件：** `archive.py` L671

```python
except Exception as e:
    return ExtractResult(..., error=f"压缩包提取异常: {e}")
```

这个 error boundary 捕获所有异常，包括 `KeyboardInterrupt`（Python 3.11+ 里 `KeyboardInterrupt` 不继承 `Exception` 所以其实 OK），但更精确的做法是 `except (OSError, zipfile.BadZipFile, ValueError)` 等具体类型。

---

### P2 — 重复代码消除（DRY）

#### 8. EPUB / MOBI 模块与 isbn_utils 的 ISBN 扫描逻辑重复

**文件：** `epub.py` L18-22, `mobi.py` L27-29

两个模块各自定义了 `_BYTE_GATE`、`_RE_ISBN_LABEL`、`_RE_ISBN_978` 正则，以及 `_clean()`、`_validate()`、`_decode()` 方法，几乎完全一样。同时 `utils/isbn_utils.py` 已有 `extract_isbn()` 做了同样的事。

**建议：**
- `_BYTE_GATE` 提到 `isbn_utils.py` 作为公共常量
- `_clean()` + `_validate()` 合并为 `isbn_utils.validate_and_format(raw)` 
- `_decode()` 提到 `utils/` 作为公共工具
- EPUB/MOBI 的 `_scan()` 改为调用 `isbn_utils.extract_isbn()`

#### 9. `_ok()` / `_fail()` 模式重复

**文件：** `epub.py` L239-253, `mobi.py` L258-273

两个模块定义了完全相同的 `_ok()` 和 `_fail()` 静态方法。

**建议：** 在 `models.py` 或 `utils/` 中提供 `ExtractResult.ok(...)` 和 `ExtractResult.fail(...)` 工厂方法，所有 Extractor 统一使用。

#### 10. `cip_rules.py` 的 `_is_valid_isbn` 与 `isbn_utils.is_valid_isbn` 重复

**文件：** `cip_rules.py` L178-181, `isbn_utils.py` L50-75

两个函数名字几乎一样但实现不同：`cip_rules` 版本只做格式检查（正则），`isbn_utils` 版本做校验和检查。容易混淆。

**建议：** `cip_rules._is_valid_isbn` 改名为 `_is_isbn_format` 或直接调用 `isbn_utils.is_valid_isbn`。

---

### P3 — API 设计

#### 11. `Batch.__init__` 有 25+ 个参数

**文件：** `batch.py` L470-504

虽然已用 `*` 将大部分参数设为 keyword-only，但 25 个参数仍然过多。根据 dignified-python 的 API 设计原则，应考虑分组。

**建议：** 引入 `BatchOptions` dataclass（或 Pydantic model），将 rename_mode / skip_isbn / skip_ssid / normalize_ext / keep_name / max_name_len 等重命名相关参数合并为一个 `rename_options: RenameOptions`，将 deduplicate / dedup_read_size 合并为 `dedup_options: DedupOptions`。

#### 12. `ArchiveExtractor.extract()` 的参数设计

**文件：** `archive.py` L517-523

```python
def extract(cls, archive_path, detector=None, *, filename=False) -> ExtractResult:
```

`detector` 作为 positional 参数但没有类型注解（只有 `Detector | None`）。其他 Extractor（PdfExtractor, EpubExtractor, MobiExtractor）也有类似问题。

**建议：** 统一所有 Extractor 的 `extract()` 签名，使用 `@classmethod` 或改为实例方法。

#### 13. `Detect` 和 `Locate` 的 `__repr__` 过于详细

**文件：** `models.py` L49-50, L113-121

手动实现 `__repr__` 拼接字符串，Pydantic 本身有 `model_dump()` 和自定义序列化能力。

**建议：** 考虑用 `__str__` 做友好输出，`__repr__` 保留默认 Pydantic 格式（含所有字段值，方便调试）。

---

### P4 — 模块设计与导入

#### 14. `pdf.py` 模块级副作用

**文件：** `pdf.py` L33-35

```python
_pymupdf._g_out_message = open(os.devnull, "w", encoding="utf-8")
_pymupdf.JM_mupdf_show_errors = 0
_pymupdf.JM_mupdf_show_warnings = 0
```

`import isbnx.pdf` 就会打开 `/dev/null` 并修改 pymupdf 全局状态。这是 import-time side effect。

**建议：** 包裹在 `@cache` 函数中，首次打开 PDF 时执行：

```python
@cache
def _suppress_mupdf_output() -> None:
    _pymupdf._g_out_message = open(os.devnull, "w", encoding="utf-8")
    _pymupdf.JM_mupdf_show_errors = 0
    _pymupdf.JM_mupdf_show_warnings = 0
```

#### 15. `__init__.py` 的 `__getattr__` 懒加载 — 合理的性能优化

**文件：** `__init__.py` L27-49

`_lazy_map` 在每次 `__getattr__` 调用时都重新创建 dict。虽然开销小，但可以提到模块级。

**建议：** 将 `_lazy_map` 提为模块级常量 `_LAZY_IMPORT_MAP`。

#### 16. `isbnx.py` 大量 inline import

**文件：** `isbnx.py` 全文

`from isbnx.utils.io import require_suffix` 在每个方法内部重复出现（L361, L401, L485）。`from isbnx.utils.filename import extract_from_filename` 也重复出现。

**建议：** 提到模块顶部。这些不是重型依赖（utils/io.py 不引入 onnxruntime），inline import 没有性能收益。

---

### P5 — 类型注解完善

#### 17. `_ArchiveReader.getinfo()` 缺少返回类型

**文件：** `archive.py` L282

（已在 P0-3 提及）

#### 18. `detector.py` 多个方法缺少返回类型

**文件：** `detector.py`

- `_preprocess()` → `tuple[np.ndarray, float, int, int]`
- `_run()` → `np.ndarray`
- `_pick_boxes()` → `list[tuple[np.ndarray, float, int]]`
- `_scale_box()` → `tuple[int, int, int, int] | None`

#### 19. `config.py` 的 `configure()` 类型宽松

**文件：** `config.py` L130

```python
def configure(**kwargs: Any) -> None:
```

`**kwargs: Any` 失去类型安全。可以用 `TypedDict` 或 `Unpack[SettingsDict]` 约束。

---

### P6 — 小改进

#### 20. `detect_pdf_type()` 将 "mixed" 归为 "scanned"

**文件：** `pdf_type.py` L53

```python
if ratio > 0:
    return "scanned"  # "mixed" 一律认为是扫描件
```

注释说"避免误判为 text_based"，但 mixed 类型的 PDF（部分页有文本）其实可以先走文本搜索再 fallback 到 ONNX。当前做法会跳过文本搜索直接走慢路径。

#### 21. `_ISBN_KEYWORD_PDF` 与 `_ISBN_KEYWORD` 重复定义

**文件：** `detector.py` L22, `pdf.py` L126

```python
# detector.py
_ISBN_KEYWORD = re.compile(r"[1Il]\s*[S5]\s*[8B]\s*N", re.IGNORECASE)
# pdf.py
_ISBN_KEYWORD_PDF = re.compile(r"[1Il]\s*[S5]\s*[8B]\s*N", re.IGNORECASE)
```

完全一样的正则。提到 `isbn_utils.py` 统一维护。

#### 22. `_SevenZipReader.__init__` 中 `import tempfile` 冗余

**文件：** `archive.py` L358

`tempfile` 已在模块顶部 import（L28），`_SevenZipReader.__init__` 内部又 import 了一次。

#### 23. `batch.py` 的 `_deduplicate()` 在非 dry-run 模式下直接 `fp.unlink()` 删除文件

**文件：** `batch.py` L999

```python
if not self.dry_run:
    fp.unlink()
```

永久删除重复文件，无回收站保护。如果用户误操作，数据不可恢复。

**建议：** 改为移动到 `_trash/` 目录或使用 `send2trash` 库。

---

### 实施优先级建议

| 批次 | 条目 | 预估工作量 |
|------|------|-----------|
| **第一批** | P0-1 (ISBN-10 限制), P0-4 (is_valid 耦合 settings) | 2h |
| **第二批** | P1-5 (异常处理收窄), P1-6 (B904) | 4h |
| **第三批** | P2-8 (ISBN扫描DRY), P2-9 (_ok/_fail), P4-14 (pdf副作用), P4-16 (inline import) | 4h |
| **第四批** | P0-2 (settings 线程安全), P3-11 (Batch 参数分组) | 6h |
| **第五批** | P5 (类型注解), P6 (小改进) | 2h |
