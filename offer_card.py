"""Render a compact offer message image matching the user's Telegram screenshot."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

W, H = 900, 770  # approximately 3x a 300px Telegram message card


def _font(size: int, bold: bool = False):
    paths = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"]
    )
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _fit_font(draw, text, max_width, size, bold=False, min_size=24):
    while size > min_size:
        font = _font(size, bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
        size -= 2
    return _font(min_size, bold)


def _round_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0]-1, size[1]-1), radius=radius, fill=255)
    return mask


def render_offer_card(
    photo_path: str | Path,
    output_path: str | Path,
    *,
    nft_name: str,
    nft_number: str,
    amount: int,
    buyer_name: str,
    receiver: str,
    remaining_hours: int,
    remaining_minutes: int,
    is_virtual: bool = False,
):
    """Compose the NFT thumbnail, offer copy and remaining-time text into one image.

    Telegram adds the real inline buttons immediately below this image.
    """
    photo_path, output_path = Path(photo_path), Path(output_path)
    with Image.open(photo_path) as source:
        gift = ImageOps.fit(source.convert("RGB"), (324, 324), method=Image.Resampling.LANCZOS)

    background = Image.new("RGB", (W, H), (9, 8, 10))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((160, -80, 740, 600), fill=(64, 37, 74, 45))
    background = Image.alpha_composite(background.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(85)))
    draw = ImageDraw.Draw(background, "RGBA")
    draw.rounded_rectangle((20, 12, W-20, H-12), radius=68, fill=(36, 29, 36, 248), outline=(255, 255, 255, 15), width=2)

    tile_size, tile_x, tile_y = 324, (W-324)//2, 48
    background.paste(gift, (tile_x, tile_y), _round_mask((tile_size, tile_size), 70))
    draw = ImageDraw.Draw(background, "RGBA")

    # Centered two-line copy like the reference screenshot.
    first = f"{buyer_name} предлагает Вам {amount} звёзд"
    second = f"за подарок {nft_name} #{nft_number}."
    draw.text((W//2, 424), first, font=_fit_font(draw, first, 790, 43, True, 25), fill=(255,255,255,255), anchor="mm")
    draw.text((W//2, 475), second, font=_fit_font(draw, second, 790, 42, True, 25), fill=(255,255,255,255), anchor="mm")

    # Initial countdown snapshot; deal expiry is independently enforced by the bot.
    timer_top = f"Предложение действует ещё {remaining_hours}ч"
    timer_bottom = f"{remaining_minutes:02d}м."
    timer_font = _fit_font(draw, timer_top, 710, 39, True, 24)
    draw.text((W//2, 575), timer_top, font=timer_font, fill=(245,245,247,255), anchor="mm")
    draw.text((W//2, 623), timer_bottom, font=_font(39, True), fill=(245,245,247,255), anchor="mm")

    if receiver.lower() != buyer_name.lstrip("@").lower():
        draw.text((W//2, 683), f"Получатель: @{receiver}", font=_fit_font(draw, f"Получатель: @{receiver}", 720, 24, False, 20), fill=(211,205,216,255), anchor="mm")
    if is_virtual:
        draw.text((W//2, 714), "ВЫПЛАТА НЕ ГАРАНТИРОВАНА", font=_font(20, True), fill=(255,205,95,255), anchor="mm")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    background.convert("RGB").save(output_path, format="JPEG", quality=91, optimize=True)
    return output_path
