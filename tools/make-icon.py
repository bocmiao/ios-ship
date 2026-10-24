#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""生成 App 图标（1024×1024，无透明通道）和 asset catalog 骨架。

为什么要专门生成：App Store 上传时 Apple 会强制检查 CFBundleIconName 和
120×120 图标，缺了直接拒收（错误码 90713 / 90022）。而这个检查发生在
**上传阶段**——编译、签名、导出全能过，所以很容易拖到最后一步才发现。

另外两个容易漏的点：
  1. 图标不能带 alpha 通道（带了一样被 90022 拒）
  2. project.yml 里必须有 ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon
     否则 asset catalog 不会被编进包，报的错看起来像 Info.plist 少了字段

用法：
  python make-icon.py                          # 输出到 <xcodeDir>/Assets.xcassets
  python make-icon.py --out App/Assets.xcassets
  python make-icon.py --glyph viewfinder        # 换风格
  python make-icon.py --color 0,217,192         # 换强调色

默认输出的是**占位图标**——它能过苹果的检查，但不代表好看。
真项目请自己设计一张 1024×1024 无 alpha 的 PNG 替换 icon-1024.png。
"""

import argparse
import json
import os
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit(
        "需要 Pillow 来画图标：\n"
        "  pip install pillow\n\n"
        "不想装也行——直接用你自己的 1024×1024 PNG 覆盖\n"
        "  <xcodeDir>/Assets.xcassets/AppIcon.appiconset/icon-1024.png\n"
        "只要保证它没有 alpha 通道即可。"
    )

SIZE = 1024
SS = 4  # 超采样倍数，先放大画再缩，边缘才干净
S = SIZE * SS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 默认强调色（青绿）。--color 可覆盖。
ACCENT = (0, 217, 192)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_line(d, p1, p2, width, fill):
    """圆头线段。PIL 的 line 默认是平头，端点在图标里会很生硬。"""
    d.line([p1, p2], fill=fill, width=width)
    r = width / 2
    for (x, y) in (p1, p2):
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def backdrop():
    """深色竖向渐变底。大面积纯黑在 OLED 主屏上显得很闷。"""
    img = Image.new("RGB", (S, S), (12, 12, 15))
    d = ImageDraw.Draw(img)
    top = (30, 30, 38)
    bottom = (8, 8, 11)
    for y in range(S):
        d.line([(0, y), (S, y)], fill=lerp(top, bottom, y / S))
    return img


def draw_ship(img):
    """一个向上的箭头 + 一道强调色底线。

    中性、和任何项目都不冲突，也符合这个工具本身在做的事（把东西送上去）。
    """
    d = ImageDraw.Draw(img)
    cx = S // 2
    white = (255, 255, 255)

    # 箭杆
    shaft_w = int(0.052 * S)
    top = int(0.255 * S)
    bottom = int(0.665 * S)
    rounded_line(d, (cx, bottom), (cx, top + int(0.06 * S)), shaft_w, white)

    # 箭头（两笔斜线，用圆头线段拼，比 polygon 干净）
    head_w = int(0.052 * S)
    head_h = int(0.155 * S)
    spread = int(0.135 * S)
    rounded_line(d, (cx, top), (cx - spread, top + head_h), head_w, white)
    rounded_line(d, (cx, top), (cx + spread, top + head_h), head_w, white)

    # 底线
    bar_w = int(0.30 * S)
    bar_y = int(0.735 * S)
    rounded_line(d, (cx - bar_w // 2, bar_y), (cx + bar_w // 2, bar_y),
                 int(0.038 * S), ACCENT)

    return img


def draw_viewfinder(img):
    """四角取景框 + 中心对焦框。相机 / 视觉类项目用这个。"""
    d = ImageDraw.Draw(img)
    margin = int(0.165 * S)
    arm = int(0.155 * S)
    lw = int(0.034 * S)
    white = (255, 255, 255)

    corners = [
        (margin, margin, +1, +1),
        (S - margin, margin, -1, +1),
        (margin, S - margin, +1, -1),
        (S - margin, S - margin, -1, -1),
    ]
    for (x, y, dx, dy) in corners:
        rounded_line(d, (x, y), (x + dx * arm, y), lw, white)
        rounded_line(d, (x, y), (x, y + dy * arm), lw, white)

    half = int(0.138 * S)
    cx = cy = S // 2
    fw = int(0.016 * S)
    gap = int(0.052 * S)  # 四角断开，像对焦框而不是普通方框

    edges = [
        ((cx - half, cy - half), (cx - gap, cy - half)),
        ((cx + gap, cy - half), (cx + half, cy - half)),
        ((cx - half, cy + half), (cx - gap, cy + half)),
        ((cx + gap, cy + half), (cx + half, cy + half)),
        ((cx - half, cy - half), (cx - half, cy - gap)),
        ((cx - half, cy + gap), (cx - half, cy + half)),
        ((cx + half, cy - half), (cx + half, cy - gap)),
        ((cx + half, cy + gap), (cx + half, cy + half)),
    ]
    for p1, p2 in edges:
        rounded_line(d, p1, p2, fw, ACCENT)

    dot = int(0.018 * S)
    d.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=ACCENT)

    return img


def draw_plain(img):
    """纯渐变，什么都不画。当底色板用。"""
    return img


GLYPHS = {
    "ship": draw_ship,
    "viewfinder": draw_viewfinder,
    "plain": draw_plain,
}


def default_out():
    """推断 Assets.xcassets 的位置。

    顺序：shipconfig 的 xcodeDir → 目录里现成的 Assets.xcassets → App/
    """
    # 1. 和 ship.py 共用同一套推断，避免两边对不上
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import shipconfig

        cfg = shipconfig.load(ROOT)
        xcode_dir = cfg.get("xcodeDir")
        if xcode_dir:
            cand = os.path.join(ROOT, xcode_dir, "Assets.xcassets")
            if os.path.isdir(os.path.dirname(cand)) or os.path.isdir(cand):
                return cand
    except Exception:
        pass

    # 2. 工程里已经有 Assets.xcassets 就直接用它（比如手写的 .xcodeproj）
    for name in sorted(os.listdir(ROOT)):
        cand = os.path.join(ROOT, name, "Assets.xcassets")
        if os.path.isdir(cand):
            return cand

    # 3. 兜底
    return os.path.join(ROOT, "App", "Assets.xcassets")


def main():
    ap = argparse.ArgumentParser(
        description="生成 1024×1024 无 alpha 的 App 图标 + asset catalog 骨架")
    ap.add_argument("--out", default="", metavar="DIR",
                    help="Assets.xcassets 目录，默认从 ship.config.json / project.yml 推断")
    ap.add_argument("--glyph", default="ship", choices=sorted(GLYPHS),
                    help="图形风格（默认 ship）")
    ap.add_argument("--color", default="", metavar="R,G,B",
                    help="强调色，默认 0,217,192")
    args = ap.parse_args()

    global ACCENT
    if args.color:
        try:
            parts = [int(x) for x in args.color.split(",")]
            if len(parts) != 3 or any(not 0 <= v <= 255 for v in parts):
                raise ValueError
            ACCENT = tuple(parts)
        except ValueError:
            sys.exit("--color 要写成 R,G,B 三个 0-255 的数，比如 0,217,192")

    assets = os.path.abspath(args.out) if args.out else default_out()
    iconset = os.path.join(assets, "AppIcon.appiconset")
    os.makedirs(iconset, exist_ok=True)

    icon = GLYPHS[args.glyph](backdrop()).resize((SIZE, SIZE), Image.LANCZOS)

    # RGB 保存 = 天然没有 alpha 通道，Apple 要的就是这个
    out_png = os.path.join(iconset, "icon-1024.png")
    icon.save(out_png, "PNG")
    print("图标已生成:", out_png, icon.size, icon.mode)

    # Xcode 14+ 的单尺寸写法：一张 1024 就够，其余尺寸由 Xcode 生成
    with open(os.path.join(iconset, "Contents.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "images": [
                    {
                        "filename": "icon-1024.png",
                        "idiom": "universal",
                        "platform": "ios",
                        "size": "1024x1024",
                    }
                ],
                "info": {"author": "xcode", "version": 1},
            },
            f,
            indent=2,
        )
        f.write("\n")

    with open(os.path.join(assets, "Contents.json"), "w", encoding="utf-8") as f:
        json.dump({"info": {"author": "xcode", "version": 1}}, f, indent=2)
        f.write("\n")

    print("Assets.xcassets 结构已就绪:", assets)
    print()
    print("别忘了 project.yml 里这一行，否则图标编不进包（报 90713）：")
    print("  ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon")


if __name__ == "__main__":
    main()
