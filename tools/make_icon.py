#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性脚本：生成 AIPhotoArrange 应用图标。

设计：
- 蓝色对角渐变背景（左上 #1e40af 深蓝 -> 右下 #3b82f6 亮蓝）
- 正方形圆角（radius ≈ 18.75% 边长）
- 前景：白色相机图标（机身圆角矩形 + 镜头同心圆 + 取景器凸起 + 闪光点）

输出：
- app.ico                含 256/128/64/48/32/16 六个尺寸
- app_icon_preview.png   256x256 预览图（供肉眼查看效果）

用法：
    python make_icon.py

生成后可删除本脚本，app.ico 为最终产物。
"""
from PIL import Image, ImageDraw


# ---------- 颜色 ----------
C_BG_TOP = (30, 64, 175)       # #1e40af 深蓝
C_BG_BOTTOM = (59, 130, 246)   # #3b82f6 亮蓝
C_WHITE = (255, 255, 255)
C_LENS_RING = (147, 197, 253)  # 浅蓝描边
C_FLASH = (255, 255, 255)


def lerp_color(c1, c2, t):
    """线性插值两个 RGB 颜色。t=0 -> c1, t=1 -> c2。"""
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def make_gradient_bg(size):
    """生成对角线渐变背景（左上 C_BG_TOP -> 右下 C_BG_BOTTOM）。"""
    img = Image.new("RGB", (size, size), C_BG_TOP)
    px = img.load()
    for y in range(size):
        for x in range(size):
            # 对角线插值：t = (x+y) / (2*(size-1))，范围 0~1
            t = (x + y) / (2 * max(size - 1, 1))
            px[x, y] = lerp_color(C_BG_TOP, C_BG_BOTTOM, t)
    return img


def make_rounded_mask(size, radius):
    """生成圆角矩形蒙版（白色区域保留，黑色透明）。"""
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def draw_camera(draw, size):
    """在 RGBA 画布上绘制白色相机图标，居中。所有尺寸按 size 等比缩放。"""
    s = size  # 别名简写

    # ---- 1. 取景器凸起（顶部小矩形）----
    # 宽度约为机身的 35%，高度约为机身的 12%，居中
    vf_w = s * 0.28
    vf_h = s * 0.08
    vf_x = (s - vf_w) / 2
    vf_y = s * 0.20
    draw.rounded_rectangle(
        [vf_x, vf_y, vf_x + vf_w, vf_y + vf_h],
        radius=vf_h * 0.4,
        fill=C_WHITE,
    )

    # ---- 2. 机身（大圆角矩形）----
    body_w = s * 0.72
    body_h = s * 0.46
    body_x = (s - body_w) / 2
    body_y = s * 0.27
    draw.rounded_rectangle(
        [body_x, body_y, body_x + body_w, body_y + body_h],
        radius=body_h * 0.20,
        fill=C_WHITE,
    )

    # ---- 3. 镜头外环（浅蓝描边圆）----
    lens_r = s * 0.188
    lens_cx = s / 2
    lens_cy = body_y + body_h * 0.52
    # 外环（浅蓝）
    draw.ellipse(
        [lens_cx - lens_r, lens_cy - lens_r,
         lens_cx + lens_r, lens_cy + lens_r],
        fill=C_LENS_RING,
    )

    # ---- 4. 镜头内圈（深蓝/背景色，形成孔洞效果）----
    inner_r = lens_r * 0.62
    # 用渐变背景的中心色填充内圈，模拟"透过镜头看到背景"
    inner_color = lerp_color(C_BG_TOP, C_BG_BOTTOM, 0.5)
    draw.ellipse(
        [lens_cx - inner_r, lens_cy - inner_r,
         lens_cx + inner_r, lens_cy + inner_r],
        fill=inner_color,
    )

    # ---- 5. 闪光点（镜头右上小白点，增加质感）----
    flash_r = s * 0.025
    flash_cx = lens_cx + lens_r * 0.45
    flash_cy = lens_cy - lens_r * 0.45
    draw.ellipse(
        [flash_cx - flash_r, flash_cy - flash_r,
         flash_cx + flash_r, flash_cy + flash_r],
        fill=C_FLASH,
    )


def render_icon(size):
    """渲染指定尺寸的图标，返回 RGBA Image。"""
    # 1. 渐变背景
    bg = make_gradient_bg(size).convert("RGBA")

    # 2. 圆角蒙版
    radius = int(size * 0.1875)  # 18.75% 圆角
    mask = make_rounded_mask(size, radius)

    # 3. 透明底 + 渐变背景（圆角外透明）
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(bg, (0, 0), mask)

    # 4. 绘制相机前景（直接画在 icon 上，相机在圆角内）
    draw = ImageDraw.Draw(icon)
    draw_camera(draw, size)

    # 5. 再次用圆角蒙版裁剪（相机取景器等可能略超圆角，确保裁干净）
    icon.putalpha(mask)

    return icon


def main():
    sizes = [256, 128, 64, 48, 32, 16]
    # 渲染各尺寸
    images = [render_icon(s) for s in sizes]

    # 1. 输出预览 PNG（256x256，供肉眼查看）
    preview = images[0].convert("RGBA")
    preview.save("app_icon_preview.png", "PNG")
    print(f"app_icon_preview.png  ({preview.size[0]}x{preview.size[1]})")

    # 2. 输出 .ico（多尺寸）
    # PIL 的 ico 保存：第一张为主图，后续为附加尺寸
    ico_path = "app.ico"
    images[0].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
    )
    print(f"app.ico  (sizes={sizes})")

    # 3. 验证 ico 内容
    with Image.open(ico_path) as ico:
        print(f"验证：ICO 文件包含尺寸 -> {ico.info.get('sizes', '未知')}")
    print("完成。请查看 app_icon_preview.png 确认设计效果。")


if __name__ == "__main__":
    main()
