import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from app.log import logger
from app.plugins.wsembycover.utils.color_helper import ColorHelper


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


def _fit_render_size(width, height, max_width=960, max_height=540):
    width = max(1, int(width))
    height = max(1, int(height))
    ratio = min(1.0, max_width / width, max_height / height)
    return max(1, int(width * ratio)), max(1, int(height * ratio))


def _build_style2_background(image_path, canvas_size, color_ratio, bg_color_config):
    with Image.open(image_path).convert("RGB") as bg_src:
        background = ImageOps.fit(bg_src, canvas_size, method=Image.Resampling.LANCZOS)
        background = ImageEnhance.Contrast(background).enhance(1.08)
        background = ImageEnhance.Color(background).enhance(1.04)
        background = background.filter(ImageFilter.GaussianBlur(radius=max(2, canvas_size[1] // 540)))

        if bg_color_config:
            base_color = ColorHelper.get_background_color(
                bg_src,
                color_mode=bg_color_config.get("mode", "auto"),
                custom_color=bg_color_config.get("custom_color"),
                config_color=bg_color_config.get("config_color"),
            )
        else:
            base_color = ColorHelper.get_background_color(bg_src)

    overlay_color = ColorHelper.darken_color(base_color, 0.66)
    frame_color = ColorHelper.lighten_color(base_color, 1.08)
    ratio = min(1.0, max(0.0, float(color_ratio)))
    canvas = background.convert("RGBA")

    left_gradient = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    grad_px = left_gradient.load()
    for x in range(canvas_size[0]):
        strength = 1.0 - min(1.0, x / max(1, int(canvas_size[0] * 0.64)))
        alpha = int((78 + 84 * ratio) * (strength ** 1.35))
        color = overlay_color + (alpha,)
        for y in range(canvas_size[1]):
            grad_px[x, y] = color
    canvas = Image.alpha_composite(canvas, left_gradient)

    bottom_shade = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(bottom_shade)
    shade_draw.rectangle(
        [(0, int(canvas_size[1] * 0.72)), (canvas_size[0], canvas_size[1])],
        fill=(18, 16, 12, 94),
    )
    bottom_shade = bottom_shade.filter(ImageFilter.GaussianBlur(radius=max(16, canvas_size[1] // 36)))
    return Image.alpha_composite(canvas, bottom_shade), frame_color


def _build_title_layers(canvas_size, title, font_path, font_size, font_offset):
    zh_font_path, en_font_path = font_path
    title_zh, title_en = title
    zh_font_size, en_font_size = font_size
    zh_font_offset, title_spacing, _ = font_offset

    text_color = (232, 208, 150, 244)
    text_shadow = (88, 56, 18, 96)

    text_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    shadow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_layer)
    shadow_draw = ImageDraw.Draw(shadow_layer)

    zh_font = ImageFont.truetype(zh_font_path, int(max(1, float(zh_font_size) * 0.45)))
    en_font = ImageFont.truetype(en_font_path, int(max(1, float(en_font_size) * 0.45)))

    title_x = int(canvas_size[0] * 0.04)
    title_y = int(canvas_size[1] * 0.13) + int(float(zh_font_offset))
    zh_bbox = draw.textbbox((0, 0), title_zh, font=zh_font)
    zh_h = zh_bbox[3] - zh_bbox[1]

    for offset in range(4, 12, 2):
        shadow_draw.text((title_x + offset, title_y + offset), title_zh, font=zh_font, fill=text_shadow)
    draw.text((title_x, title_y), title_zh, font=zh_font, fill=text_color)

    en_text = (title_en or "").upper()
    if en_text:
        en_spacing = max(4, int(float(en_font_size) * 0.16))
        en_y = title_y + zh_h + int(float(title_spacing))
        for offset in range(2, 8, 2):
            _draw_spaced_text(
                shadow_draw,
                (title_x + offset, en_y + offset),
                en_text,
                en_font,
                text_shadow,
                en_spacing,
            )
        _draw_spaced_text(draw, (title_x, en_y), en_text, en_font, text_color, en_spacing)

    return text_layer, shadow_layer


def _encode_apng_under_limit(frames, frame_duration, limit_bytes):
    normalized = [f.convert("RGB") for f in frames]
    base_size = normalized[0].size
    normalized = [f if f.size == base_size else f.resize(base_size, Image.Resampling.LANCZOS) for f in normalized]
    buffer = BytesIO()
    normalized[0].save(
        buffer,
        format="PNG",
        save_all=True,
        append_images=normalized[1:],
        duration=frame_duration,
        loop=0,
        optimize=True,
        compress_level=9,
    )
    data = buffer.getvalue()
    if len(data) > limit_bytes:
        logger.warning(f"static_3 APNG 超过体积限制: {len(data) / 1024 / 1024:.2f}MB > {limit_bytes / 1024 / 1024:.0f}MB")
    return data


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
        if not image_path:
            logger.warning("static_3 缺少背景图路径")
            return False

        width = 1920
        height = 1080
        if resolution_config:
            width = int(getattr(resolution_config, "width", width))
            height = int(getattr(resolution_config, "height", height))
        canvas_size = _fit_render_size(width, height, max_width=960, max_height=540)
        base_canvas, frame_color = _build_style2_background(
            image_path=image_path,
            canvas_size=canvas_size,
            color_ratio=color_ratio,
            bg_color_config=bg_color_config,
        )
        text_layer, shadow_layer = _build_title_layers(
            canvas_size=canvas_size,
            title=title,
            font_path=font_path,
            font_size=font_size,
            font_offset=font_offset,
        )

        poster_paths = []
        for index in range(1, 21):
            candidate = Path(library_dir) / f"{index}.jpg"
            if candidate.exists():
                poster_paths.append(candidate)
        if not poster_paths:
            logger.warning("static_3 未找到可用海报图")
            return False

        while len(poster_paths) < 20:
            poster_paths.extend(poster_paths[: 20 - len(poster_paths)])
        poster_paths = poster_paths[:20]

        poster_height = int(canvas_size[1] * 0.43)
        poster_width = int(poster_height / 1.43)
        gap = max(8, int(canvas_size[0] * 0.008))
        cards = [_build_poster_card(path, (poster_width, poster_height), frame_color) for path in poster_paths]

        max_card_h = max(card.size[1] for card in cards)
        start_y = canvas_size[1] - max_card_h - int(canvas_size[1] * 0.028)
        slot_width = cards[0].size[0] + gap
        strip_width = slot_width * len(cards)
        speed_px_s = (canvas_size[0] + cards[0].size[0]) / 8.0
        cycle_seconds = 30.0
        cycle_distance = int(speed_px_s * cycle_seconds)
        fps = 30
        frame_duration = int(1000 / fps)
        frame_count = max(80, int(cycle_seconds * fps))

        frames = []
        for frame_idx in range(frame_count):
            canvas = base_canvas.copy()
            progress = frame_idx / max(1, frame_count)
            shift = int(progress * cycle_distance)
            x_anchor = canvas_size[0] + gap - shift

            for loop_idx in range(-1, 3):
                base_x = x_anchor + loop_idx * strip_width
                for idx, card in enumerate(cards):
                    card_x = base_x + idx * slot_width
                    if card_x > canvas_size[0] or card_x + card.size[0] < 0:
                        continue
                    canvas.paste(card, (card_x, start_y), card)

            merged = Image.alpha_composite(
                canvas,
                shadow_layer.filter(ImageFilter.GaussianBlur(radius=max(6, canvas_size[1] // 160))),
            )
            merged = Image.alpha_composite(merged, text_layer)
            frames.append(merged)

        apng_bytes = _encode_apng_under_limit(frames, frame_duration, limit_bytes=20 * 1024 * 1024)
        if apng_bytes:
            logger.info(f"static_3 APNG体积: {len(apng_bytes) / 1024:.1f}KB")
            if len(apng_bytes) > 2 * 1024 * 1024:
                logger.warning("static_3 APNG 超过 2MB，已使用最小化压缩档")
            return base64.b64encode(apng_bytes).decode("utf-8")
        return False
    except Exception as e:
        logger.error(f"创建 static_3 封面时出错: {e}")
        return False
