# ascii2png

将 Unicode 框线字符画（`┌─┐│└─┘▼↔` 等）解析为方框/箭头/文字结构，再用 matplotlib 渲染成矢量 PNG。

## 依赖

### Python 包

```bash
pip install matplotlib
```

Python 标准库：`argparse`、`dataclasses`、`pathlib`、`__future__`

### 字体

脚本启动时会自动搜索系统中已安装的中文字体，支持 Noto CJK、WenQuanYi、
Microsoft YaHei 等常见字体，无需手动配置。搜索结果覆盖 Linux / macOS / Windows (WSL) 各平台。

如果自动搜索失败（或想用特定字体），通过 `--font` 参数指定路径。

如需安装新字体，参考以下命令：

```bash
# Arch Linux
sudo pacman -S noto-fonts-cjk

# Ubuntu / Debian
sudo apt install fonts-noto-cjk

# Fedora
sudo dnf install google-noto-cjk-fonts

# macOS
brew install font-noto-sans-cjk
```

## 用法

```bash
python ascii2png.py --out output.png
python ascii2png.py --out-dir ./images --filename diagram.png --font-size 16 --dpi 300
```

### 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--out` | — | 输出图片完整路径（优先级最高） |
| `--out-dir` | `../source/resource` | 输出目录 |
| `--filename` | `architecture-overview-flow.png` | 输出文件名 |
| `--font` | 自动检测 | 字体路径，不指定则搜索系统中已安装的中文字体 |
| `--font-size` | `14` | 字号 |
| `--dpi` | `200` | 图片 DPI |

## 自定义字符画

编辑 `ascii2png.py` 末尾的 `DIAGRAM_TEXT` 变量，将新的字符画粘贴进去即可。
