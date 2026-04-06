import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from app.log import logger
from app.plugins.mediacovergenerator.utils.color_helper import ColorHelper


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
    shadow_offset = max(10, int(card_h * 0.02))
    shadow_blur = max(14, int(card_w * 0.04))

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
    shadow = _add_shadow(card, shadow_offset, shadow_blur, 115)
    shadow_canvas.paste(shadow, (shadow_blur, shadow_blur), shadow)
    shadow_canvas.paste(card, (shadow_blur, shadow_blur), card)
    return shadow_canvas


def create_style_static_5(
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
        if not image_path:
            logger.warning("static_5 缺少背景图路径")
            return False

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

        bg_src = Image.open(image_path).convert("RGB")
        background = ImageOps.fit(bg_src, canvas_size, method=Image.Resampling.LANCZOS)
        blur_radius = max(12, int(float(blur_size) * (canvas_size[1] / 2400.0)))
        background = background.filter(ImageFilter.GaussianBlur(radius=blur_radius))

        if bg_color_config:
            base_color = ColorHelper.get_background_color(
                bg_src,
                color_mode=bg_color_config.get("mode", "auto"),
                custom_color=bg_color_config.get("custom_color"),
                config_color=bg_color_config.get("config_color"),
            )
        else:
            base_color = ColorHelper.get_background_color(bg_src)

        overlay_color = ColorHelper.darken_color(base_color, 0.75)
        frame_color = ColorHelper.lighten_color(base_color, 1.12)
        text_color = (248, 232, 170, 235)
        text_shadow = ColorHelper.darken_color(base_color, 0.55) + (110,)

        ratio = float(color_ratio)
        ratio = min(1.0, max(0.0, ratio))
        tint_layer = Image.new("RGBA", canvas_size, overlay_color + (int(150 * ratio),))
        canvas = Image.alpha_composite(background.convert("RGBA"), tint_layer)

        haze = Image.new("RGBA", canvas_size, (255, 248, 226, 0))
        haze_draw = ImageDraw.Draw(haze)
        haze_draw.ellipse(
            [
                int(canvas_size[0] * 0.34),
                int(canvas_size[1] * -0.10),
                int(canvas_size[0] * 1.02),
                int(canvas_size[1] * 0.95),
            ],
            fill=(255, 245, 220, 92),
        )
        haze = haze.filter(ImageFilter.GaussianBlur(radius=max(24, canvas_size[1] // 30)))
        canvas = Image.alpha_composite(canvas, haze)

        bottom_glow = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(bottom_glow)
        glow_draw.rectangle(
            [(0, int(canvas_size[1] * 0.72)), (canvas_size[0], canvas_size[1])],
            fill=ColorHelper.darken_color(base_color, 0.8) + (88,),
        )
        bottom_glow = bottom_glow.filter(ImageFilter.GaussianBlur(radius=max(28, canvas_size[1] // 28)))
        canvas = Image.alpha_composite(canvas, bottom_glow)

        text_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        shadow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(text_layer)
        shadow_draw = ImageDraw.Draw(shadow_layer)

        zh_font = ImageFont.truetype(zh_font_path, int(max(1, float(zh_font_size))))
        en_font = ImageFont.truetype(en_font_path, int(max(1, float(en_font_size))))

        title_x = int(canvas_size[0] * 0.035)
        title_y = int(canvas_size[1] * 0.14) + int(float(zh_font_offset))
        zh_bbox = draw.textbbox((0, 0), title_zh, font=zh_font)
        zh_h = zh_bbox[3] - zh_bbox[1]

        for offset in range(4, 13, 2):
            shadow_draw.text((title_x + offset, title_y + offset), title_zh, font=zh_font, fill=text_shadow)
        draw.text((title_x, title_y), title_zh, font=zh_font, fill=text_color)

        en_y = title_y + zh_h + int(float(title_spacing))
        en_spacing = max(4, int(float(en_font_size) * 0.16))
        for offset in range(2, 8, 2):
            _draw_spaced_text(
                shadow_draw,
                (title_x + offset, en_y + offset),
                title_en.upper(),
                en_font,
                text_shadow,
                en_spacing,
            )
        _draw_spaced_text(draw, (title_x, en_y), title_en.upper(), en_font, text_color, en_spacing)

        poster_paths = []
        for index in range(1, 6):
            candidate = Path(library_dir) / f"{index}.jpg"
            if candidate.exists():
                poster_paths.append(candidate)
        if not poster_paths:
            logger.warning("static_5 未找到可用海报图")
            return False

        available_width = int(canvas_size[0] * 0.965)
        gap = max(14, int(canvas_size[0] * 0.010))
        poster_width = int((available_width - gap * 4) / 5)
        poster_height = int(poster_width * 1.43)
        cards = [_build_poster_card(path, (poster_width, poster_height), frame_color) for path in poster_paths[:5]]

        cards_total_width = sum(card.size[0] for card in cards) + gap * (len(cards) - 1)
        start_x = max(0, (canvas_size[0] - cards_total_width) // 2)
        start_y = canvas_size[1] - max(card.size[1] for card in cards) - int(canvas_size[1] * 0.03)

        for card in cards:
            canvas.paste(card, (start_x, start_y), card)
            start_x += card.size[0] + gap

        merged = Image.alpha_composite(
            canvas,
            shadow_layer.filter(ImageFilter.GaussianBlur(radius=max(8, canvas_size[1] // 135))),
        )
        merged = Image.alpha_composite(merged, text_layer)

        buffer = BytesIO()
        merged.save(buffer, format="PNG", optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as e:
        logger.error(f"创建静态5封面时出错: {e}")
        return False
