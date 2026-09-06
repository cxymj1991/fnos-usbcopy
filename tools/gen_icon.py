"""生成应用图标（纯标准库，无需 Pillow）。

风格参照飞牛 fnOS 官方桌面应用：圆角矩形 + 蓝色渐变底 + 白色简洁图形。
- 大圆角（rc ≈ 0.28*size，飞牛桌面图标观感）
- 垂直渐变：浅蓝 #4F8FFF → 深蓝 #1A5BE0
- 中央白色同步环 + 上下箭头（保留 USB Copy 同步的语义）

输出：
  ICON.PNG        (64x64，包根目录)
  ICON_256.PNG    (256x256，包根目录)
  app/ui/images/icon-64.png
  app/ui/images/icon-256.png
"""

import os
import zlib
import struct
import math


def new_buf(w, h):
    return bytearray(w * h * 4)


def setpx(buf, w, h, x, y, rgba):
    if 0 <= x < w and 0 <= y < h:
        i = (y * w + x) * 4
        buf[i] = rgba[0]
        buf[i + 1] = rgba[1]
        buf[i + 2] = rgba[2]
        buf[i + 3] = rgba[3]


def fill_rounded_rect_gradient(buf, w, h, rc, c_top, c_bot):
    """圆角矩形 + 垂直线性渐变。

    矩形由中心矩形 + 四条平直边 + 四个圆角组成。判断逻辑：
    - 中心列带 (x 在 [rc, w-1-rc])：所有 y 都在矩形内（包括顶/底平直边）。
    - 中心行带 (y 在 [rc, h-1-rc])：所有 x 都在矩形内（包括左/右平直边）。
    - 四个角：只有到对应角圆心距离 <= rc 才在矩形内（圆弧）。
    """
    for y in range(h):
        t = y / max(1, h - 1)
        col = (
            int(c_top[0] + (c_bot[0] - c_top[0]) * t),
            int(c_top[1] + (c_bot[1] - c_top[1]) * t),
            int(c_top[2] + (c_bot[2] - c_top[2]) * t),
            255,
        )
        in_y = (rc <= y <= h - 1 - rc)
        for x in range(w):
            inside = False
            if rc <= x <= w - 1 - rc or in_y:
                inside = True
            else:
                cx = rc if x < w // 2 else w - 1 - rc
                cy = rc if y < h // 2 else h - 1 - rc
                dx = x - cx
                dy = y - cy
                if dx * dx + dy * dy <= rc * rc:
                    inside = True
            if inside:
                setpx(buf, w, h, x, y, col)


def fill_ring(buf, w, h, cx, cy, r_out, r_in, rgba):
    for y in range(h):
        for x in range(w):
            dx = x - cx
            dy = y - cy
            d = math.sqrt(dx * dx + dy * dy)
            if r_in <= d <= r_out:
                setpx(buf, w, h, x, y, rgba)


def _sign(a, b, c):
    return (a[0] - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (a[1] - c[1])


def fill_triangle(buf, w, h, p1, p2, p3, rgba):
    xs = [p1[0], p2[0], p3[0]]
    ys = [p1[1], p2[1], p3[1]]
    minx = max(0, int(min(xs)))
    maxx = min(w - 1, int(max(xs)))
    miny = max(0, int(min(ys)))
    maxy = min(h - 1, int(max(ys)))
    for y in range(miny, maxy + 1):
        for x in range(minx, maxx + 1):
            pt = (x, y)
            d1 = _sign(pt, p1, p2)
            d2 = _sign(pt, p2, p3)
            d3 = _sign(pt, p3, p1)
            neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
            pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
            if not (neg and pos):
                setpx(buf, w, h, x, y, rgba)


def write_png(path, w, h, buf):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(buf[y * w * 4:(y + 1) * w * 4])
    comp = zlib.compress(bytes(raw), 9)

    def chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        c += struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff)
        return c

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)))
        f.write(chunk(b"IDAT", comp))
        f.write(chunk(b"IEND", b""))


def make(size):
    """生成一个 size×size 的飞牛风格应用图标。"""
    w = h = size
    buf = new_buf(w, h)
    # 蓝色渐变：飞牛品牌色系（亮蓝 → 中深蓝）
    c_top = (79, 143, 255)    # #4F8FFF
    c_bot = (26, 91, 224)     # #1A5BE0
    # 大圆角：约占 22% 边长，飞牛桌面图标观感（需 < 25% 避免相邻角圆重叠成四瓣）
    rc = int(size * 0.22)
    fill_rounded_rect_gradient(buf, w, h, rc, c_top, c_bot)

    # 中央白色同步环 + 上下箭头（USB Copy 同步的语义标识）。
    # 环占约 44% 直径，飞牛风格简洁不切碎背景。
    white = (255, 255, 255, 255)
    cx = cy = size // 2
    r_out = int(size * 0.24)
    r_in = int(size * 0.175)
    fill_ring(buf, w, h, cx, cy, r_out, r_in, white)
    a = int(size * 0.06)
    L = int(size * 0.07)
    fill_triangle(buf, w, h,
                  (cx, cy - r_out - L), (cx - a, cy - r_out), (cx + a, cy - r_out), white)
    fill_triangle(buf, w, h,
                  (cx, cy + r_out + L), (cx - a, cy + r_out), (cx + a, cy + r_out), white)
    return buf


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    pkg = os.path.abspath(os.path.join(base, ".."))
    ui_img = os.path.join(pkg, "app", "ui", "images")
    os.makedirs(ui_img, exist_ok=True)
    specs = [
        (64, "ICON.PNG", "icon-64.png"),
        (256, "ICON_256.PNG", "icon-256.png"),
    ]
    for size, root_name, ui_name in specs:
        buf = make(size)
        write_png(os.path.join(pkg, root_name), size, size, buf)
        write_png(os.path.join(ui_img, ui_name), size, size, buf)
        print("wrote", root_name, "and", ui_name)
