import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from app.log import logger


def _draw_spaced_text(draw, position, text, font, fill, spacing):
    x, y = position
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        bbox = draw.textbbox((x, y), char, font=font)
        x += (bbox[2] - bbox[0]) + spacing


def _add_shadow(image, offset_y, blur_radius, alpha):
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        [(0, offset_y), (image.size[0] - 1, image.size[1] - 1)],
        radius=max(1, image.size[0] // 15),
        fill=(0, 0, 0, alpha),
    )
    return shadow.filter(ImageFilter.GaussianBlur(radius=blur_radius))


def _build_poster_card(image_path, size, border_color):
    card_w, card_h = size
    radius = max(18, int(card_w * 0.075))
    border_width = max(4, int(card_w * 0.015))
    shadow_offset = max(6, int(card_h * 0.012))
    shadow_blur = max(16, int(card_w * 0.045))

    with Image.open(image_path).convert("RGB") as src:
        poster = ImageOps.fit(src, (card_w, card_h), method=Image.Resampling.LANCZOS)

    card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
    poster_rgba = poster.convert("RGBA")

    outer_mask = Image.new("L", (card_w, card_h), 0)
    ImageDraw.Draw(outer_mask).rounded_rectangle(
        [(0, 0), (card_w - 1, card_h - 1)],
        radius=radius,
        fill=255,
    )

    inner_mask = Image.new("L", (card_w, card_h), 0)
    ImageDraw.Draw(inner_mask).rounded_rectangle(
        [
            (border_width, border_width),
            (card_w - border_width - 1, card_h - border_width - 1),
        ],
        radius=max(1, radius - border_width),
        fill=255,
    )

    border_layer = Image.new("RGBA", (card_w, card_h), border_color + (255,))
    border_layer.putalpha(outer_mask)
    card = Image.alpha_composite(card, border_layer)

    inner_poster = ImageOps.fit(
        poster_rgba,
        (card_w - border_width * 2, card_h - border_width * 2),
        method=Image.Resampling.LANCZOS,
    )
    inner_card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
    inner_card.paste(inner_poster, (border_width, border_width), inner_poster)
    inner_card.putalpha(inner_mask)
    card = Image.alpha_composite(card, inner_card)

    shadow_canvas = Image.new(
        "RGBA",
        (card_w + shadow_blur * 2, card_h + shadow_blur * 2 + shadow_offset),
        (0, 0, 0, 0),
    )
    shadow = _add_shadow(card, shadow_offset, shadow_blur, 92)
    shadow_canvas.paste(shadow, (shadow_blur, shadow_blur), shadow)
    shadow_canvas.paste(card, (shadow_blur, shadow_blur), card)
    return shadow_canvas


def _build_tech_blue_background(canvas_size):
    width, height = canvas_size
    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 255))
    draw = ImageDraw.Draw(canvas)

    top_color = (10, 34, 88)
    bottom_color = (7, 15, 40)
    for y in range(height):
        blend = y / max(1, height - 1)
        color = tuple(
            int(top_color[i] * (1 - blend) + bottom_color[i] * blend)
            for i in range(3)
        )
        draw.line([(0, y), (width, y)], fill=color + (255,))

    glow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_draw.ellipse(
        [
            int(width * -0.08),
            int(height * -0.10),
            int(width * 0.46),
            int(height * 0.42),
        ],
        fill=(70, 170, 255, 105),
    )
    glow_draw.ellipse(
        [
            int(width * 0.58),
            int(height * 0.04),
            int(width * 1.10),
            int(height * 0.58),
        ],
        fill=(40, 125, 235, 82),
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=max(30, height // 20)))
    canvas = Image.alpha_composite(canvas, glow_layer)

    lines = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    line_draw = ImageDraw.Draw(lines)
    for idx in range(8):
        y = int(height * (0.16 + idx * 0.08))
        line_draw.rounded_rectangle(
            [int(width * 0.08), y, int(width * 0.92), y + max(2, height // 420)],
            radius=2,
            fill=(90, 190, 255, 30),
        )
    for idx in range(9):
        x = int(width * (0.12 + idx * 0.09))
        line_draw.rounded_rectangle(
            [x, int(height * 0.10), x + max(2, width // 700), int(height * 0.74)],
            radius=2,
            fill=(90, 190, 255, 26),
        )
    lines = lines.filter(ImageFilter.GaussianBlur(radius=1.5))
    canvas = Image.alpha_composite(canvas, lines)

    shade_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade_layer)
    shade_draw.rectangle(
        [(0, int(height * 0.72)), (width, height)],
        fill=(6, 9, 20, 86),
    )
    shade_layer = shade_layer.filter(ImageFilter.GaussianBlur(radius=max(18, height // 28)))
    return Image.alpha_composite(canvas, shade_layer)


def create_style_static_3(
    image_path,
    library_dir,
    title,
    font_path,
    font_size=(170, 75),
    font_offset=(0, 40, 40),
    blur_size=50,
    color_ratio=0.8,
    resolution_config=None,
    bg_color_config=None,
):
    try:
        zh_font_path, en_font_path = font_path
        title_zh, title_en = title
        zh_font_size, en_font_size = font_size
        zh_font_offset, title_spacing, _ = font_offset

        width = 1920
        height = 1080
        if resolution_config:
            width = int(getattr(resolution_config, "width", width))
            height = int(getattr(resolution_config, "height", height))
        canvas_size = (max(1, width), max(1, height))

        canvas = _build_tech_blue_background(canvas_size)

        zh_font = ImageFont.truetype(zh_font_path, int(max(1, float(zh_font_size))))
        en_font = ImageFont.truetype(en_font_path, int(max(1, float(en_font_size))))

        # 鎏金色系
        text_color = (232, 201, 120, 255)
        text_shadow = (88, 56, 16, 128)

        text_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        shadow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(text_layer)
        shadow_draw = ImageDraw.Draw(shadow_layer)

        zh_bbox = draw.textbbox((0, 0), title_zh, font=zh_font)
        zh_w = zh_bbox[2] - zh_bbox[0]
        zh_h = zh_bbox[3] - zh_bbox[1]
        title_x = (canvas_size[0] - zh_w) // 2
        title_y = int(canvas_size[1] * 0.12) + int(float(zh_font_offset))

        for offset in range(4, 12, 2):
            shadow_draw.text((title_x + offset, title_y + offset), title_zh, font=zh_font, fill=text_shadow)
        draw.text((title_x, title_y), title_zh, font=zh_font, fill=text_color)

        en_text = (title_en or "").upper()
        if en_text:
            en_spacing = max(4, int(float(en_font_size) * 0.16))
            en_width = 0
            for idx, ch in enumerate(en_text):
                cb = draw.textbbox((0, 0), ch, font=en_font)
                en_width += (cb[2] - cb[0]) + (en_spacing if idx < len(en_text) - 1 else 0)
            en_x = (canvas_size[0] - en_width) // 2
            en_y = title_y + zh_h + int(float(title_spacing))

            for offset in range(2, 8, 2):
                _draw_spaced_text(shadow_draw, (en_x + offset, en_y + offset), en_text, en_font, text_shadow, en_spacing)
            _draw_spaced_text(draw, (en_x, en_y), en_text, en_font, text_color, en_spacing)

        poster_paths = []
        for index in range(1, 6):
            candidate = Path(library_dir) / f"{index}.jpg"
            if candidate.exists():
                poster_paths.append(candidate)
        if not poster_paths:
            logger.warning("static_3 未找到可用海报图")
            return False

        available_width = int(canvas_size[0] * 0.90)
        poster_width = int(available_width / 5.45)
        poster_height = int(poster_width * 1.43)
        frame_color = (214, 181, 106)
        cards = [_build_poster_card(path, (poster_width, poster_height), frame_color) for path in poster_paths[:5]]

        total_cards_width = sum(card.size[0] for card in cards)
        gap = max(12, int((canvas_size[0] - total_cards_width) / 6))
        start_x = gap
        start_y = canvas_size[1] - max(card.size[1] for card in cards) - int(canvas_size[1] * 0.035)

        for card in cards:
            canvas.paste(card, (start_x, start_y), card)
            start_x += card.size[0] + gap

        merged = Image.alpha_composite(
            canvas,
            shadow_layer.filter(ImageFilter.GaussianBlur(radius=max(8, canvas_size[1] // 135))),
        )
        merged = Image.alpha_composite(merged, text_layer)

        # 静态输出 JPEG，尽量减小上传体积
        buffer = BytesIO()
        merged.convert("RGB").save(buffer, format="JPEG", quality=88, optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as e:
        logger.error(f"创建 static_3 封面时出错: {e}")
        return False
