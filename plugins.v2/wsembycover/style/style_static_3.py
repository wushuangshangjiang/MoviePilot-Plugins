import base64
import math
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


def _measure_spaced_text(draw, text, font, spacing):
    width = 0
    height = 0
    for index, char in enumerate(text):
        bbox = draw.textbbox((0, 0), char, font=font)
        char_w = bbox[2] - bbox[0]
        char_h = bbox[3] - bbox[1]
        width += char_w
        if index < len(text) - 1:
            width += spacing
        height = max(height, char_h)
    return width, height


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
    shadow = _add_shadow(card, shadow_offset, shadow_blur, 98)
    shadow_canvas.paste(shadow, (shadow_blur, shadow_blur), shadow)
    shadow_canvas.paste(card, (shadow_blur, shadow_blur), card)
    return shadow_canvas


def _ease_in_out(progress):
    progress = max(0.0, min(1.0, progress))
    return 0.5 - 0.5 * math.cos(math.pi * progress)


def _build_background(canvas_size, frame_index, frame_count):
    width, height = canvas_size
    background = Image.new("RGBA", canvas_size, (0, 0, 0, 255))
    draw = ImageDraw.Draw(background)

    top_color = (230, 245, 255)
    bottom_color = (117, 194, 255)
    for y in range(height):
        blend = y / max(1, height - 1)
        color = tuple(
            int(top_color[i] * (1 - blend) + bottom_color[i] * blend)
            for i in range(3)
        )
        draw.line([(0, y), (width, y)], fill=color + (255,))

    progress = frame_index / max(1, frame_count - 1)

    glow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    for idx, alpha in enumerate((74, 60, 48), start=1):
        center_x = int(width * (0.15 + 0.28 * idx) + math.sin(progress * math.pi * 2 + idx) * width * 0.16)
        center_y = int(height * (0.12 + 0.18 * idx))
        radius_x = int(width * (0.18 + idx * 0.03))
        radius_y = int(height * (0.11 + idx * 0.025))
        glow_draw.ellipse(
            [
                center_x - radius_x,
                center_y - radius_y,
                center_x + radius_x,
                center_y + radius_y,
            ],
            fill=(255, 255, 255, alpha),
        )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=max(24, height // 18)))
    background = Image.alpha_composite(background, glow_layer)

    streak_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    streak_draw = ImageDraw.Draw(streak_layer)
    streak_specs = [
        (0.18, 0.10, 0.22, 0.42, (240, 251, 255, 88), 0.0),
        (0.44, 0.20, 0.26, 0.48, (196, 236, 255, 78), 0.9),
        (0.72, 0.08, 0.22, 0.40, (255, 255, 255, 72), 1.8),
    ]
    for center_ratio, top_ratio, width_ratio, height_ratio, color, phase in streak_specs:
        offset = math.sin(progress * math.pi * 2 + phase) * width * 0.12
        center_x = int(width * center_ratio + offset)
        top_y = int(height * top_ratio)
        streak_w = int(width * width_ratio)
        streak_h = int(height * height_ratio)
        streak_draw.rounded_rectangle(
            [
                center_x - streak_w // 2,
                top_y,
                center_x + streak_w // 2,
                top_y + streak_h,
            ],
            radius=max(24, streak_w // 3),
            fill=color,
        )
    streak_layer = streak_layer.filter(ImageFilter.GaussianBlur(radius=max(30, height // 20)))
    background = Image.alpha_composite(background, streak_layer)

    bottom_glow = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    bottom_draw = ImageDraw.Draw(bottom_glow)
    bottom_draw.rectangle(
        [(0, int(height * 0.72)), (width, height)],
        fill=(36, 84, 128, 62),
    )
    bottom_glow = bottom_glow.filter(ImageFilter.GaussianBlur(radius=max(20, height // 24)))
    return Image.alpha_composite(background, bottom_glow)


def create_style_static_6(
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

        zh_font = ImageFont.truetype(zh_font_path, int(max(1, float(zh_font_size))))
        en_font = ImageFont.truetype(en_font_path, int(max(1, float(en_font_size))))

        poster_paths = []
        for index in range(1, 6):
            candidate = Path(library_dir) / f"{index}.jpg"
            if candidate.exists():
                poster_paths.append(candidate)
        if not poster_paths:
            logger.warning("static_6 未找到可用海报图")
            return False

        available_width = int(canvas_size[0] * 0.90)
        poster_width = int(available_width / 5.45)
        poster_height = int(poster_width * 1.43)
        frame_color = (224, 245, 255)
        cards = [_build_poster_card(path, (poster_width, poster_height), frame_color) for path in poster_paths[:5]]

        total_cards_width = sum(card.size[0] for card in cards)
        gap = max(12, int((canvas_size[0] - total_cards_width) / 6))
        base_start_x = gap
        start_y = canvas_size[1] - max(card.size[1] for card in cards) - int(canvas_size[1] * 0.035)
        move_distance = max(36, int(canvas_size[0] * 0.045))

        text_color = (248, 252, 255, 255)
        text_shadow = (32, 74, 116, 130)
        accent_line = (255, 255, 255, 110)

        measure_image = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        measure_draw = ImageDraw.Draw(measure_image)

        zh_bbox = measure_draw.textbbox((0, 0), title_zh, font=zh_font)
        zh_w = zh_bbox[2] - zh_bbox[0]
        zh_h = zh_bbox[3] - zh_bbox[1]
        en_text = title_en.upper()
        en_spacing = max(4, int(float(en_font_size) * 0.16))
        en_w, en_h = _measure_spaced_text(measure_draw, en_text, en_font, en_spacing)

        title_x = (canvas_size[0] - zh_w) // 2
        title_y = int(canvas_size[1] * 0.12) + int(float(zh_font_offset))
        en_x = (canvas_size[0] - en_w) // 2
        en_y = title_y + zh_h + int(float(title_spacing))

        frame_count = 120
        frame_duration = int(round(1000 / 30))
        frames = []

        for frame_index in range(frame_count):
            progress = frame_index / max(1, frame_count - 1)
            canvas = _build_background(canvas_size, frame_index, frame_count)

            deco_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
            deco_draw = ImageDraw.Draw(deco_layer)
            deco_draw.rounded_rectangle(
                [
                    int(canvas_size[0] * 0.18),
                    int(canvas_size[1] * 0.09),
                    int(canvas_size[0] * 0.82),
                    int(canvas_size[1] * 0.11),
                ],
                radius=max(8, canvas_size[1] // 120),
                fill=accent_line,
            )
            deco_layer = deco_layer.filter(ImageFilter.GaussianBlur(radius=max(10, canvas_size[1] // 90)))
            canvas = Image.alpha_composite(canvas, deco_layer)

            shadow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
            shadow_draw = ImageDraw.Draw(shadow_layer)
            text_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
            text_draw = ImageDraw.Draw(text_layer)

            for offset in range(4, 12, 2):
                shadow_draw.text(
                    (title_x + offset, title_y + offset),
                    title_zh,
                    font=zh_font,
                    fill=text_shadow,
                )
            text_draw.text((title_x, title_y), title_zh, font=zh_font, fill=text_color)

            for offset in range(2, 8, 2):
                _draw_spaced_text(
                    shadow_draw,
                    (en_x + offset, en_y + offset),
                    en_text,
                    en_font,
                    text_shadow,
                    en_spacing,
                )
            _draw_spaced_text(text_draw, (en_x, en_y), en_text, en_font, text_color, en_spacing)

            start_x = base_start_x
            max_delay = 0.32
            delay_step = max_delay / max(1, len(cards) - 1)
            for idx, card in enumerate(cards):
                delay = (len(cards) - 1 - idx) * delay_step
                local_progress = 0.0
                if progress > delay:
                    local_progress = (progress - delay) / max(0.001, 1.0 - max_delay)
                card_shift = int(move_distance * _ease_in_out(local_progress))
                card_y = start_y
                canvas.paste(card, (start_x - card_shift, card_y), card)
                start_x += card.size[0] + gap

            merged = Image.alpha_composite(
                canvas,
                shadow_layer.filter(ImageFilter.GaussianBlur(radius=max(8, canvas_size[1] // 135))),
            )
            merged = Image.alpha_composite(merged, text_layer)
            frames.append(merged)

        if not frames:
            return False

        buffer = BytesIO()
        frames[0].save(
            buffer,
            format="PNG",
            save_all=True,
            append_images=frames[1:],
            duration=frame_duration,
            loop=0,
            optimize=False,
            disposal=2,
        )
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as e:
        logger.error(f"创建 static_6 封面时出错: {e}")
        return False
