# isbnx

从 PDF / 图片 / EPUB / MOBI / 压缩包文件中提取 ISBN 号，支持 ONNX 深度学习检测和 OCR 识别。

## 快速开始

```python
from isbnx import extract

# 统一入口：按后缀自动分发
result = extract("cover.png")
if result.success:
    print(result.bookinfo.isbn13)  # 9787123456789

# 优先从文件名提取（跳过内容扫描，更快）
result = extract("9787123456789_三体.epub", filename=True)

# 也可以直接调用具体方法
from isbnx import ISBNX

result = ISBNX().from_pdf("book.pdf")
result = ISBNX().from_archive("book.zip")
result = ISBNX().from_epub("book.epub")
result = ISBNX().from_mobi("book.mobi")
```

## 安装

```bash
pip install isbnx
# 或
uv add isbnx
```

## 支持格式

| 类型 | 格式 | 提取策略 |
|------|------|----------|
| **图片** | PNG / JPG / WebP / BMP / PDG | ONNX 检测 → OCR / 条码解码 |
| **PDF** | PDF | 书签定位 + 文本搜索（文本型）→ 渲染图片检测（扫描件） |
| **EPUB** | EPUB | OPF 元数据优先 → XHTML 内容扫描 |
| **MOBI** | MOBI | EXTH 元数据优先 → 文本记录扫描 |
| **压缩包** | ZIP / RAR / 7Z / UVZ | meta.xml → bookinfo.dat → leg001.pdg → 兜底 PDG 图片 |

## 核心流程

1. **ONNX 模型检测** — YOLO 模型定位图片中的 ISBN 区域
2. **OCR 识别** — RapidOCR 引擎识别文字，pyzbar 解码条码
3. **ISBN 提取** — 正则匹配 + 校验 ISBN 合法性
4. **SSID 回退** — 压缩包 bookinfo.dat 中的 SS 号可作为标识

## 严格等级

| 等级 | 含义 | 场景 |
|:----:|------|------|
| 1 | ISBN + SSID 都必须存在 | 最高严格 |
| 2 | ISBN 必须存在且有效 | 图片/PDF 检测 |
| 3 | ISBN 有效 或 SSID 存在 | 压缩包（默认） |

## 配置参数

通过 `ISBNX(config=...)` 传入自定义 `Settings`：

```python
from isbnx.config import Settings

# 切换严格等级
config = Settings(strict=2)
# 或修改子配置
config.ocr.ocr_model = "medium"
config.detector.conf_threshold = 0.5

result = ISBNX(config=config).from_image("cover.png")
```

## 批量处理

对整个目录进行批量 ISBN 提取与文件整理：

```python
from isbnx.batch import Batch, BatchConfig

# 默认配置
result = Batch(
    source_dir="D:/books",
    success_dir="D:/books/done",
    failed_dir="D:/books/unrecognized",
).run()

# 自定义配置
config = BatchConfig(
    rename_mode=1,
    extensions={".epub", ".pdf"},
    max_workers=4,
)
result = Batch("D:/books", "D:/ok", "D:/fail", config=config).run()
```

主要功能：

- **多线程并行** — ThreadPoolExecutor，自动适配 CPU 核数，降低 ONNX 锁争抢
- **文件名预检** — 文件名已有 ISBN/SSID 的跳过内容提取，大幅提速
- **4 种重命名模式** — 追加/前置、替换/保留旧标识，灵活控制
- **文件去重** — 集成 `dedupx` 按 inode/大小/哈希去重
- **干运行模式** — 通过 `try_run=True` 先预览，确认后再实际移动
- **CSV 报告** — 可选输出详细处理记录
- **进度回调** — 支持 `entries_callback` 驱动外部 UI
- **优雅终止** — 通过 `shutdown_event` 从外部安全取消批量任务
- **保留目录结构** — `keep_tree=True` 在目标目录保留源目录层级

## 文件分发规则

`extract()` 会先根据文件后缀快速判断提取路径：

- 图片：`.png` / `.jpg` / `.jpeg` / `.webp` / `.bmp` / `.pdg`
- PDF：`.pdf`
- EPUB：`.epub`
- MOBI：`.mobi`
- 压缩包：`.zip` / `.rar` / `.uvz` / `.7z`

这一步是轻量预检，不做内容嗅探；真正的文件内容校验仍由对应提取器负责。 