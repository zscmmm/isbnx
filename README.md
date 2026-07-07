
# isbnx

> 从 PDF、图片、EPUB、MOBI 和压缩包中智能提取 ISBN 号

[![Python](https://img.shields.io/badge/python-≥3.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Documentation](https://img.shields.io/badge/docs-mkdocs-blue)](https://isbnx.readthedocs.org)



---

## 安装

```bash
pip install isbnx
# 或
uv add isbnx
```

### 系统依赖

- pyzbar, [参考安装](https://pypi.org/project/pyzbar/), 自己简单测试准确率好像比[zbarlight](https://pypi.org/project/zbarlight/)好一些,单安装依赖有点麻烦

    - **win**: 需要一些c++动态库, 
    - **macOS**: 需要设置 `DYLD_LIBRARY_PATH` 以支持条形码解码

    ```bash
    echo 'export DYLD_LIBRARY_PATH=/opt/homebrew/lib:$DYLD_LIBRARY_PATH' >> ~/.zshrc
    ```


- pdf-inspector, 可选, [参考安装](https://github.com/firecrawl/pdf-inspector), 需要配置rust,不然可能安装不成功




---

## 快速开始

### 统一入口

```python
from isbnx import extract

# 自动根据文件后缀选择提取方式
result = extract("cover.png")
if result.success:
    print(result.bookinfo.isbn13)  # 9787123456789
```

### 使用 ISBNX 类

```python
from isbnx import ISBNX

# 从图片提取
result = ISBNX().from_image("cover.png")

# 从 PDF 提取（支持文本型和扫描件）
result = ISBNX().from_pdf("book.pdf")

# 从 EPUB 提取
result = ISBNX().from_epub("book.epub")

# 从 MOBI 提取
result = ISBNX().from_mobi("book.mobi")

# 从压缩包提取（支持 ZIP/RAR/UVZ）
result = ISBNX().from_archive("book.zip")

# 或使用统一的自动分发入口
result = ISBNX().extract("book.pdf")
```

---

## 支持的文件格式

| 类型 | 后缀 | 提取策略 |
|------|------|----------|
| **图片** | `.png` `.jpg` `.jpeg` | ONNX 检测 → OCR / 条码解码 |
| **PDF** | `.pdf` | 书签定位 + 文本搜索（文本型）→ 渲染图片检测（扫描件） |
| **EPUB** | `.epub` | OPF 元数据优先 → XHTML 内容扫描 |
| **MOBI** | `.mobi` | EXTH 元数据优先 → 文本记录扫描 |
| **压缩包** | `.zip` `.rar` `.uvz` | `meta.xml` → `bookinfo.dat` → `leg001.pdg` → 兜底 PDG 图片 |

---

## 核心流程

```
输入文件
    │
    ▼
文件类型判断 ───→ 图片 ───→ ONNX YOLO 检测 ───→ OCR / 条码解码 ───→ ISBN 提取
    │              PDF ───→ 类型判断（文本/扫描）─→ 文本搜索 / 渲染检测
    │              EPUB ──→ OPF 元数据 / XHTML 扫描
    │              MOBI ──→ EXTH 元数据 / 文本记录扫描
    │              压缩包 ─→ meta.xml → bookinfo.dat → PDG 图片解码 → 检测
    │
    ▼
 ExtractResult
  ├── success: bool          # 是否提取成功
  ├── bookinfo.isbn13        # ISBN-13
  ├── bookinfo.isbn10        # ISBN-10
  ├── bookinfo.ssid          # SS 号（压缩包特有）
  ├── locate                 # 定位信息（页码、方法）
  ├── ocr                    # OCR 原始识别文本
  ├── meta                   # 文件元信息
  └── elapsed                # 耗时（秒）
```

---

## 配置

### 严格等级

通过 `strict` 参数控制提取结果的校验严格程度：

| 等级 | 含义 | 适用场景 |
|:----:|------|----------|
| 1 | ISBN **和** SSID 都必须存在且有效 | 最高严格 |
| 2 | ISBN 必须存在且校验通过 | 图片/PDF 检测 |
| 3 | ISBN 有效 **或** SSID 存在（默认） | 压缩包提取 |

### 自定义配置

```python
from isbnx import ISBNX
from isbnx.config import Settings

# 嵌套配置通过 dict 传参
config = Settings(
    strict=2,                           # 严格等级
    ocr={"ocr_model": "medium"},        # OCR 精度（small / medium）
    detector={"conf_threshold": 0.5},   # ONNX 检测阈值
    pdf={"front_start": 1, "back_end": 3},
)

result = ISBNX(config=config).from_image("cover.png")
```

### 运行时调整

```python
from isbnx.config import configure

configure(
    strict=2,
    ocr={"ocr_model": "medium"},
    detector={"conf_threshold": 0.5},
)
```

---

## 输出模型

所有提取结果使用 Pydantic 模型，可通过一致的接口访问：

说明：当前 ONNX 模型输出为 end2end 解码后的 ``(1, 300, 6)`` 候选框，
每行格式为 ``[x1, y1, x2, y2, score, class_id]``。导出时 ``nms=True`` 不可用，
因此代码里会对这些候选框再做一次按类 NMS。

```python
result = ISBNX().from_image("cover.png")

# 基本状态
result.success     # True / False
result.error       # 失败原因（如有）

# 书籍信息
result.bookinfo.isbn       # 原始 ISBN
result.bookinfo.isbn13     # ISBN-13 格式
result.bookinfo.isbn10     # ISBN-10 格式
result.bookinfo.isbn_valid # 校验是否合法
result.bookinfo.ssid       # SS 号（压缩包）

# 定位信息
result.locate.page         # 命中页码
result.locate.method       # 定位方式（onnx / text / bookmark / ...）
result.locate.score        # 检测置信度
result.locate.candidates   # 所有 ONNX 候选框（仅 onnx 方法时有值）

# 保存候选裁剪图
result.save()              # 默认保存到源文件同名目录
result.save("output/crops")

# OCR 结果
result.ocr.lines           # OCR 文本行
result.ocr.text            # 全部文本（换行拼接）

# 元信息
result.meta.source         # 源文件路径
result.meta.source_type    # 源文件类型

# 耗时
result.elapsed             # 处理耗时（秒）
```

---

## 项目结构

```
isbnx/
├── src/
│   └── isbnx/
│       ├── __init__.py        # 公开 API 入口
│       ├── isbnx.py           # ISBNX 主类，统一提取接口
│       ├── config.py          # Pydantic-Settings 配置管理
│       ├── models.py          # Pydantic 数据模型
│       ├── detector.py        # ONNX YOLO 检测器 + 检测/OCR 流水线
│       ├── pdf.py             # PDF ISBN 提取
│       ├── epub.py            # EPUB ISBN 提取
│       ├── mobi.py            # MOBI ISBN 提取
│       ├── archive.py         # 压缩包（PDG）ISBN 提取
│       ├── pdf_type.py        # PDF 类型检测（文本/扫描）
│       ├── model/             # ONNX 模型文件
│       │   └── best.onnx
│       ├── ocr/               # OCR 引擎
│       │   ├── isbnx_pyzbar.py    # 条形码解码
│       │   └── isbnx_rapiocr.py   # RapidOCR 文字识别
│       ├── pdgview/           # PDG 图片解码器
│       └── utils/             # 工具函数
│           ├── cip_rules.py   # CIP 数据提取规则
│           ├── io.py          # 文件 I/O 工具
│           └── isbn_utils.py  # ISBN 提取与校验
├── tests/                     # 测试用例
├── docs/                      # MkDocs 文档
├── pyproject.toml
└── README.md
```

---

## 开发

### 环境要求

- Python ≥ 3.10, 只在 python 3.13上进行测试开发的
- [uv](https://docs.astral.sh/uv/) 包管理器

### 本地开发

```bash
# 克隆项目
git clone https://github.com/zscmmm/isbnx.git
cd isbnx

# 创建虚拟环境并安装依赖
uv sync --group dev

# 运行测试
uv run pytest

# 构建文档
uv run mkdocs serve
```

---

## 基准测试

`isbnx` 与同类工具 `cipx` 的对比测试报告详见 [isbnx vs cipx 报告](https://isbnx.readthedocs.org/isbnx_vs_cipx_report/)。

---

## 路线图

- [x] 图片 ISBN 提取
- [x] PDF 提取（文本型 + 扫描件）
- [x] EPUB 提取
- [x] MOBI 提取
- [x] 压缩包（PDG）提取
- [x] ONNX YOLO 深度学习检测
- [x] 条形码解码
- [x] 文档网站（MkDocs）
- [ ] LLM 解析 CIP 字段

---

## 许可证

本项目基于 MIT 许可证开源 — 详见 [LICENSE](LICENSE) 文件。

---

## 相关项目

- [mneia-isbn](https://pypi.org/project/mneia-isbn/) — ISBN 解析与校验库
- [pyzbar](https://pypi.org/project/pyzbar/) -条形码解析库
- [cipx](https://github.com/zscmmm/cipx) — CIP 信息提取工具
