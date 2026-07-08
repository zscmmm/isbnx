# isbnx

从 PDF / 图片 / 压缩包 / EPUB 文件中提取 ISBN 号，支持 ONNX 深度学习检测和 OCR 识别。

## 快速开始

```python
from isbnx import extract

# 统一入口：按后缀自动分发
result = extract("cover.png")
if result.success:
    print(result.bookinfo.isbn13)  # 9787123456789

# 也可以直接调用具体方法
from isbnx import ISBNX

result = ISBNX().from_pdf("book.pdf")

# 从压缩包提取
result = ISBNX().from_archive("book.zip")

# 从 EPUB 提取
result = ISBNX().from_epub("book.epub")
```

## 安装

```bash
pip install isbnx
# 或
uv add isbnx
```

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

## 文件分发规则

`extract()` 会先根据文件后缀快速判断提取路径：

- 图片：`.png` / `.jpg` / `.jpeg` / `.webp` / `.bmp`
- PDF：`.pdf`
- EPUB：`.epub`
- MOBI：`.mobi`
- 压缩包：`.zip` / `.rar` / `.uvz`

这一步是轻量预检，不做内容嗅探；真正的文件内容校验仍由对应提取器负责。

## 未完成

- LLM解析 