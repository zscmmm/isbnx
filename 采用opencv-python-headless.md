# RapidOCR 使用 opencv-python-headless 替换 opencv-python 降低 Nuitka 打包体积

## 背景

项目使用：

* uv 管理 Python 环境
* Nuitka 打包 Windows onefile
* RapidOCR 做 OCR
* PyMuPDF 处理 PDF

Nuitka 打包后发现体积较大，其中 OpenCV 是主要体积来源。

OpenCV 文件：

```
cv2.pyd                         ≈86 MB
opencv_videoio_ffmpeg500_64.dll ≈30 MB
```

其中：

```
opencv_videoio_ffmpeg500_64.dll
```

主要用于 OpenCV 的视频处理功能（VideoCapture、VideoWriter 等），而 OCR 场景通常不需要。

---

# 问题分析

依赖关系：

```
项目
 └── isbnx
      └── rapidocr
           └── opencv-python
```

RapidOCR 默认依赖：

```
opencv-python
```

但是 RapidOCR 实际使用 OpenCV 的功能主要是：

* resize
* rotate
* cvtColor
* findContours
* warpAffine
* fillPoly
* bitwise 操作

这些属于图像处理模块。

没有使用：

* cv2.imshow
* cv2.waitKey
* cv2.namedWindow
* cv2.VideoCapture
* cv2.VideoWriter

因此不需要完整 OpenCV。

---

# opencv-python 和 opencv-python-headless 区别

两个包提供相同 Python 接口：

```python
import cv2
```

使用方式完全一致。

区别：

| 功能       | opencv-python | opencv-python-headless |
| -------- | ------------- | ---------------------- |
| cv2 API  | ✅             | ✅                      |
| resize   | ✅             | ✅                      |
| rotate   | ✅             | ✅                      |
| OCR 图像处理 | ✅             | ✅                      |
| GUI 窗口   | ✅             | ❌                      |
| Qt/GTK   | ✅             | ❌                      |
| 视频相关组件   | ✅             | 精简                     |

对于服务器、OCR、AI 推理场景：

推荐：

```
opencv-python-headless
```

---

# uv 环境替换过程

## 1. 移除显式依赖

原：

```toml
dependencies = [
    "opencv-python>=4.x",
]
```

删除：

```toml
"opencv-python>=4.x"
```

原因：

项目本身不直接使用 OpenCV。

---

## 2. 添加 headless 版本

执行：

```powershell
uv add opencv-python-headless
```

安装后检查：

```powershell
uv pip list | findstr /i opencv

uv sync
```

结果：

```
opencv-python-headless 5.0.0.93
```

---

## 3. 验证 cv2

测试：

```powershell
uv run python -c "import cv2; print(cv2.__version__)"
```

输出：

```
5.0.0
```

说明：

headless 提供正常的 cv2 模块。

---

## 4. 验证 RapidOCR

测试：

```powershell
uv run python -c "from rapidocr import RapidOCR; print('rapidocr ok')"
```

输出：

```
rapidocr ok
```

说明：

RapidOCR 可以正常运行。

---

# 关于 uv dependency-overrides

注意：

`dependency-overrides` 不能用于：

```
opencv-python
        ↓
opencv-python-headless
```

因为：

```
opencv-python
opencv-python-headless
```

是两个不同的 Python distribution。

override 只能覆盖：

例如：

```
numpy>=1.0
```

变成：

```
numpy==2.0
```

不能替换包名。

---

# Nuitka 打包优化

替换前：

```
opencv-python

包含：
cv2.pyd
opencv_videoio_ffmpeg500_64.dll
```

替换后：

```
opencv-python-headless

去掉：
opencv_videoio_ffmpeg500_64.dll
```

预计：

```
Nuitka onefile

132MB
 ↓
约100MB
```

---

# 最终状态

环境：

```
rapidocr
   |
   └── cv2
        |
        └── opencv-python-headless
```

验证：

```powershell
uv pip list | findstr /i opencv
```

结果：

```
opencv-python-headless 5.0.0.93
```

测试：

```powershell
uv run python -c "import cv2; print(cv2.__version__)"
```

以及：

```powershell
uv run python -c "from rapidocr import RapidOCR; print('rapidocr ok')"
```

均正常。

---

# 结论

对于：

* OCR
* PDF 文字识别
* 图片处理
* ONNX 推理

`opencv-python-headless` 是 `opencv-python` 的合理替代。

它保持：

```python
import cv2
```

接口不变，同时减少不必要的视频和 GUI 组件，更适合 Nuitka 打包部署。
