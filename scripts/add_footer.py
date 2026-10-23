#!/usr/bin/env python3
"""Append a vector brand footer to visible PDF pages without covering the source."""
from __future__ import annotations

import argparse
import copy
import io
import json
import math
import os
import re
from pathlib import Path
import tempfile
from urllib.parse import urlparse

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import ArrayObject, FloatObject, NameObject, NullObject, RectangleObject
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.lib.utils import ImageReader
from PIL import Image


SKILL_ROOT = Path(__file__).resolve().parents[1]


def merge_dict(base, updates):
    result = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def asset_path(value, base):
    if value.startswith("skill://"):
        relative = Path(value[len("skill://"):])
        if relative.is_absolute() or not relative.parts or any(p in (".", "..") for p in relative.parts):
            raise ValueError("skill:// asset must be a relative path inside this skill")
        resolved = (SKILL_ROOT / relative).resolve()
        if not resolved.is_relative_to(SKILL_ROOT.resolve()):
            raise ValueError("skill:// asset escapes this skill directory")
        return resolved
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def load_config(path):
    config = json.loads((SKILL_ROOT / "assets/default-config.json").read_text())
    base = SKILL_ROOT / "assets"
    if path:
        path = Path(path).expanduser().resolve()
        config = merge_dict(config, json.loads(path.read_text()))
        base = path.parent
    if config["brand"].get("logo_image"):
        logo = asset_path(config["brand"]["logo_image"], base)
        if not logo.is_file():
            raise ValueError(f"Missing original brand logo: {logo}")
        config["brand"]["logo_image"] = str(logo)
    if not isinstance(config.get("metrics"), list) or len(config["metrics"]) > 3:
        raise ValueError("metrics must be an array containing at most three items")
    for metric in config["metrics"]:
        if not isinstance(metric.get("value"), str) or not isinstance(metric.get("label"), str):
            raise ValueError("Every metric requires string value and label")
    if urlparse(config["website"]["url"]).scheme != "https" or not urlparse(config["website"]["url"]).netloc:
        raise ValueError("website.url must be an absolute HTTPS URL")
    if not config["phone"]["uri"].startswith("tel:"):
        raise ValueError("phone.uri must start with tel:")
    for key in ("wecom", "mini_program"):
        item = config["qr"][key]
        if item.get("image"):
            p = asset_path(item["image"], base)
            if not p.is_file():
                raise ValueError(f"Missing original QR image: {p}")
            item["image"] = str(p.resolve())
        if item.get("link") and not item["link"].startswith("https://"):
            raise ValueError(f"qr.{key}.link must be HTTPS when supplied")
    style = config["style"]
    layout = style.get("layout", "grouped")
    if layout not in ("grouped", "reference-columns", "reference-sentence"):
        raise ValueError("style.layout must be grouped, reference-columns or reference-sentence")
    if style.get("endorsement_decoration", "none") not in ("none", "silver-laurel"):
        raise ValueError("style.endorsement_decoration must be none or silver-laurel")
    if layout == "reference-sentence":
        spans = config.get("statement_spans")
        if not isinstance(spans, list) or not spans:
            raise ValueError("reference-sentence requires nonempty statement_spans")
        for span in spans:
            if not isinstance(span, dict) or not isinstance(span.get("text"), str):
                raise ValueError("Every statement span requires string text")
            if "bold" in span and not isinstance(span["bold"], bool):
                raise ValueError("statement_spans.bold must be a boolean")
    if not math.isfinite(float(style["height_ratio"])) or not 0.08 <= float(style["height_ratio"]) <= 0.5:
        raise ValueError("style.height_ratio must be between 0.08 and 0.5")
    if style.get("height_pt") is not None and (not math.isfinite(float(style["height_pt"])) or float(style["height_pt"]) <= 0):
        raise ValueError("style.height_pt must be positive")
    for key in ("background", "accent", "text", "muted", "line"):
        HexColor(style[key])
    for key in ("font_path", "font_bold_path"):
        if style.get(key):
            p = asset_path(style[key], base)
            if not p.is_file():
                raise ValueError(f"Missing font: {p}")
            style[key] = str(p.resolve())
    suffix = config.get("output_suffix", "-branded")
    if not isinstance(suffix, str) or not suffix or "/" in suffix or "\\" in suffix:
        raise ValueError("output_suffix must be a nonempty filename suffix")
    return config


def register_fonts(config):
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    font = "STSong-Light"
    bold = "STSong-Light"
    configured = config["style"].get("font_path")
    candidates = [Path(configured)] if configured else [
        Path.home() / "Library/Fonts/NotoSansSC.ttf",
        Path.home() / "Library/Fonts/NotoSansSC-Regular.ttf",
        Path("/Library/Fonts/NotoSansSC.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf"),
        Path("/usr/local/share/fonts/NotoSansSC-Regular.ttf"),
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont("BrandFooterRegular", str(candidate)))
        except Exception:
            if configured:
                raise
            continue
        font = "BrandFooterRegular"
        bold = font
        break
    if config["style"].get("font_bold_path"):
        pdfmetrics.registerFont(TTFont("BrandFooterBold", config["style"]["font_bold_path"]))
        bold = "BrandFooterBold"
    return font, bold


def write_text(c, text, x, y, size, color, font, max_width=None, align="left"):
    text = str(text)
    if max_width and pdfmetrics.stringWidth(text, font, size) > max_width:
        size *= max_width / pdfmetrics.stringWidth(text, font, size)
    c.setFillColor(color)
    c.setFont(font, size)
    if align == "center":
        c.drawCentredString(x, y, text)
    else:
        c.drawString(x, y, text)
    return size


def draw_original_code(c, path, x, y, side):
    # Preserve the complete source image. No crop, recolor or quiet-zone removal.
    with Image.open(path) as im:
        image = im.convert("RGBA")
        iw, ih = image.size
        scale = min(side / iw, side / ih)
        dw, dh = iw * scale, ih * scale
        c.drawImage(ImageReader(image), x + (side - dw) / 2, y + (side - dh) / 2,
                    width=dw, height=dh, mask="auto")


def draw_website_code(c, url, x, y, side):
    widget = QrCodeWidget(url, barLevel="M", barBorder=4)
    bx, by, rx, ty = widget.getBounds()
    drawing = Drawing(side, side, transform=[side / (rx - bx), 0, 0, side / (ty - by), -bx, -by])
    drawing.add(widget)
    renderPDF.draw(drawing, c, x, y)


def draw_metric(c, value, label, x, y, color, muted, font, max_width=195):
    match = re.fullmatch(r"([^0-9]*)([0-9][0-9,.]*)(.*)", value)
    if match:
        prefix, number, unit = match.groups()
        parts = [(prefix, font, 16), (number, "Helvetica-Bold", 32), (unit, font, 16)]
        gap = 3
        total = sum(pdfmetrics.stringWidth(part, face, size) for part, face, size in parts if part)
        total += gap * (sum(bool(part) for part, _, _ in parts) - 1)
        factor = min(1, max_width / total) if total else 1
        cursor = x
        for part, face, size in parts:
            if not part:
                continue
            write_text(c, part, cursor, y, size * factor, color, face)
            cursor += pdfmetrics.stringWidth(part, face, size * factor) + gap * factor
    else:
        write_text(c, value, x, y, 32, color, font, max_width)
    # Wrap facts rather than squeezing their statistical wording to tiny type.
    lines = []
    for paragraph in label.split("\n"):
        line = ""
        for character in paragraph:
            if line and pdfmetrics.stringWidth(line + character, font, 14) > max_width:
                lines.append(line)
                line = character
            else:
                line += character
        lines.append(line)
    for i, line in enumerate(lines):
        write_text(c, line, x, y - 30 - 18 * i, 14, muted, font)


def make_grouped_footer(width, height, config, fonts):
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(width, height), pageCompression=1)
    # Coordinate system keeps relative typography stable on portrait and landscape pages.
    sx, sy = width / 1440, height / 180
    scale = min(sx, sy)
    ox, oy = (width - 1440 * scale) / 2, (height - 180 * scale) / 2
    style = config["style"]
    c.setFillColor(HexColor(style["background"]))
    c.rect(0, 0, width, height, fill=1, stroke=0)
    c.setStrokeColor(HexColor(style["line"]))
    c.setLineWidth(0.6)
    c.line(0, height - 0.3, width, height - 0.3)
    c.translate(ox, oy)
    c.scale(scale, scale)
    font, bold = fonts
    accent = HexColor(style["accent"])
    text = HexColor(style["text"])
    muted = HexColor(style["muted"])
    line = HexColor(style["line"])
    code_items = [config["qr"]["wecom"], config["qr"]["mini_program"]]
    if config["qr"]["website"].get("enabled"):
        code_items.append(dict(config["qr"]["website"], website=True))
    logo_path = config["brand"].get("logo_image")
    if logo_path:
        with Image.open(logo_path) as original:
            logo = original.convert("RGBA")
            iw, ih = logo.size
            logo_scale = min(138 / iw, 54 / ih)
            dw, dh = iw * logo_scale, ih * logo_scale
            c.drawImage(ImageReader(logo), 64, 108 + (54 - dh) / 2,
                        width=dw, height=dh, mask="auto")
    else:
        write_text(c, config["brand"]["name"], 64, 123, 36, accent, bold, 138)
    write_text(c, config["brand"].get("tagline", ""), 238, 124, 23, text, font, 610)
    metrics = config["metrics"]
    metric_step = 270 if len(metrics) <= 3 else 0
    for i, item in enumerate(metrics):
        draw_metric(c, item["value"], item["label"], 64 + i * metric_step, 54, text, muted, font, 240)

    # The complete contact group shares a single horizontal center.
    side, gap = 96, 24
    code_group_width = side * len(code_items) + gap * (len(code_items) - 1)
    first_code_x = 1376 - code_group_width
    contact_center = first_code_x + code_group_width / 2
    phone_label = config["phone"].get("label", "电话咨询")
    label_size, phone_size = 13, 20
    phone_label_width = pdfmetrics.stringWidth(phone_label, font, label_size)
    phone_width = pdfmetrics.stringWidth(config["phone"]["display"], "Helvetica-Bold", phone_size)
    row_width = phone_label_width + 14 + phone_width
    row_factor = min(1, code_group_width / row_width)
    label_size *= row_factor
    phone_size *= row_factor
    phone_label_width *= row_factor
    row_width *= row_factor
    row_x = contact_center - row_width / 2
    write_text(c, phone_label, row_x, 158, label_size, muted, font)
    phone_x = row_x + phone_label_width + 14 * row_factor
    write_text(c, config["phone"]["display"], phone_x, 156, phone_size, text, "Helvetica-Bold")
    phone_end = phone_x + pdfmetrics.stringWidth(config["phone"]["display"], "Helvetica-Bold", phone_size)
    c.linkURL(config["phone"]["uri"], (row_x - 3, 151, phone_end + 3, 175), relative=1, thickness=0)
    for i, item in enumerate(code_items):
        x, y = first_code_x + (side + gap) * i, 44
        cx = x + side / 2
        c.setFillColor(white)
        c.rect(x, y, side, side, fill=1, stroke=0)
        if item.get("website"):
            draw_website_code(c, config["website"]["url"], x, y, side)
        elif item.get("image"):
            draw_original_code(c, item["image"], x, y, side)
        else:
            c.setStrokeColor(line)
            c.setLineWidth(1)
            c.roundRect(x + 0.5, y + 0.5, side - 1, side - 1, 4, fill=0, stroke=1)
            placeholder = item.get("placeholder", "码图待替换")
            if "\n" in placeholder:
                placeholder_lines = placeholder.split("\n")
            elif placeholder.endswith("待替换"):
                placeholder_lines = [placeholder[:-3], "待替换"]
            else:
                placeholder_lines = [placeholder]
            for j, placeholder_line in enumerate(placeholder_lines):
                baseline = y + side / 2 + (len(placeholder_lines) - 1) * 10 - 5 - 20 * j
                write_text(c, placeholder_line, cx, baseline, 13.5, muted, font,
                           side - 14, align="center")
        if item.get("website"):
            label = item["label"]
            label_width = pdfmetrics.stringWidth(label, font, 13.5)
            label_x = cx - (label_width + 17) / 2
            write_text(c, label, label_x, 21, 13.5, accent, font)
            arrow_x = label_x + label_width + 8
            c.setStrokeColor(accent)
            c.setLineWidth(1)
            c.line(arrow_x, 22, arrow_x + 7, 29)
            c.line(arrow_x + 1, 29, arrow_x + 7, 29)
            c.line(arrow_x + 7, 29, arrow_x + 7, 23)
        else:
            write_text(c, item["label"], cx, 21, 13.5, muted, font, 114, align="center")
        link = config["website"]["url"] if item.get("website") else item.get("link")
        if link:
            c.linkURL(link, (x - 4, 15, x + side + 4, y + side + 3), relative=1, thickness=0)
    if not config["qr"]["website"].get("enabled"):
        label = config["website"].get("display") or config["website"]["label"]
        write_text(c, label, 874, 24, 14, accent, font, 140)
        c.linkURL(config["website"]["url"], (872, 20, 1016, 43), relative=1, thickness=0)
    c.showPage()
    c.save()
    out.seek(0)
    return PdfReader(out).pages[0]


def draw_reference_logo(c, config, fonts):
    font, bold = fonts
    logo_path = config["brand"].get("logo_image")
    if logo_path:
        with Image.open(logo_path) as original:
            logo = original.convert("RGBA")
            iw, ih = logo.size
            factor = min(136 / iw, 53 / ih)
            dw, dh = iw * factor, ih * factor
            c.drawImage(ImageReader(logo), 64, 105 + (53 - dh) / 2,
                        width=dw, height=dh, mask="auto")
    else:
        write_text(c, config["brand"]["name"], 64, 121, 36,
                   HexColor(config["style"]["accent"]), bold, 136)


def draw_silver_text(c, text, x, y, size, font):
    # PDF text itself forms the clip: it remains searchable and selectable.
    c.saveState()
    text_object = c.beginText(x, y)
    text_object.setFont(font, size)
    text_object.setTextRenderMode(7)
    text_object.textOut(text)
    c.drawText(text_object)
    c.linearGradient(x, y, x, y + size * 0.78,
                     [HexColor("#626C78"), HexColor("#3F454D")])
    c.restoreState()


def draw_silver_laurel(c, x, y, height, mirrored=False):
    c.saveState()
    scale = height / 32
    c.translate(x + (12 * scale if mirrored else 0), y)
    c.scale(-scale if mirrored else scale, scale)
    leaves = c.beginPath()
    for bx, by, tx, ty, width in (
            (8.6, 4, 0.5, 9.5, 2.8), (5.6, 10, 0, 17, 2.9),
            (4.3, 17.5, 1.2, 25.2, 2.8), (5, 24, 7.6, 32, 2.6),
            (4.5, 14, 11, 21, 2.9)):
        dx, dy = tx - bx, ty - by
        length = math.hypot(dx, dy)
        nx, ny = -dy * width / length, dx * width / length
        leaves.moveTo(bx, by)
        leaves.curveTo(bx + dx * 0.6 + nx * 0.55, by + dy * 0.6 + ny * 0.55,
                       tx + nx * 0.45, ty + ny * 0.45, tx, ty)
        leaves.curveTo(tx - nx * 0.6, ty - ny * 0.6,
                       bx + dx * 0.4 - nx * 0.7, by + dy * 0.4 - ny * 0.7, bx, by)
        leaves.close()
    c.saveState()
    c.clipPath(leaves, stroke=0, fill=0)
    c.linearGradient(0, 0, 0, 32, [HexColor("#8F969F"), HexColor("#CDD1D6")])
    c.restoreState()
    stem = c.beginPath()
    stem.moveTo(10, 1)
    stem.curveTo(2, 10, 2, 22, 7, 31)
    c.setStrokeColor(HexColor("#ADB2B8"))
    c.setLineWidth(0.65)
    c.drawPath(stem, fill=0, stroke=1)
    c.restoreState()


def draw_reference_metrics(c, config, font):
    text, muted = (HexColor(config["style"][key]) for key in ("text", "muted"))
    decorated = config["style"].get("endorsement_decoration") == "silver-laurel"
    for x, metric in zip((250, 430, 610), config["metrics"]):
        match = re.fullmatch(r"([^0-9]*)([0-9][0-9,.]*)(.*)", metric["value"])
        parts = [(metric["value"], font, 28)] if not match else [
            (match[1], font, 16), (match[2], "Helvetica", 28), (match[3], font, 16)]
        parts = [part for part in parts if part[0]]
        total = sum(pdfmetrics.stringWidth(part, face, size) for part, face, size in parts)
        total += 3 * max(0, len(parts) - 1)
        factor = min(1, 210 / total) if total else 1
        cursor = x
        for part, face, size in parts:
            if decorated and face == "Helvetica":
                draw_silver_text(c, part, cursor, 130, size * factor, face)
            else:
                write_text(c, part, cursor, 130, size * factor, text, face)
            cursor += pdfmetrics.stringWidth(part, face, size * factor) + 3 * factor
        if decorated:
            draw_silver_laurel(c, x - 20, 126, 30)
            draw_silver_laurel(c, x + total * factor + 9, 126, 30, mirrored=True)
        lines = metric["label"].split("\n")
        for i, label in enumerate(lines):
            write_text(c, label, x, 103 - 18 * i, 14, muted, font, 210)


def draw_reference_statement(c, spans, font, color, decorated=False):
    # Keep the phrase editable/vector; a numeric span uses an actual bold face.
    # Chinese text and units remain regular even if a caller marks a larger span.
    size, leading, max_width = 20, 29, 664
    lines, line, used = [], [], 0
    for span in spans:
        for character in span["text"]:
            if character == "\n":
                lines.append(line)
                line, used = [], 0
                continue
            numeric = character.isascii() and (character.isdigit() or character in ",.%+-/")
            face = "Helvetica-Bold" if span.get("bold") and numeric else font
            advance = pdfmetrics.stringWidth(character, face, size)
            if line and used + advance > max_width:
                lines.append(line)
                line, used = [], 0
            line.append((character, face, advance))
            used += advance
    if line:
        lines.append(line)
    if len(lines) > 2:
        raise ValueError("statement_spans is too long for the two-line reference-sentence layout")
    if decorated:
        width = max(sum(character[2] for character in line) for line in lines)
        bottom = 108 if len(lines) == 2 else 136
        height = 50 if len(lines) == 2 else 25
        draw_silver_laurel(c, 250 - 8 - 12 * height / 32, bottom, height)
        draw_silver_laurel(c, 250 + width + 9, bottom, height, mirrored=True)
    for index, line in enumerate(lines):
        cursor = 250
        for character, face, advance in line:
            if decorated and face == "Helvetica-Bold":
                draw_silver_text(c, character, cursor, 140 - leading * index, size, face)
            else:
                write_text(c, character, cursor, 140 - leading * index, size, color, face)
            cursor += advance


def draw_reference_actions(c, config, font):
    # A 100% line box with more breathing room at the rounded ends.
    # Measure content instead of leaving fixed-width space beside the phone.
    size, padding_x, padding_y, gap, action_gap = 24, 16, 8, 12, 16
    button_x, button_y = 64, 22
    button_h = size + 2 * padding_y
    ascent, descent = pdfmetrics.getAscentDescent(font, size)
    baseline = button_y + padding_y + (size - ascent - descent) / 2
    button_label = config["website"].get("button_label") or config["website"].get("label", "访问官网")
    button_w = pdfmetrics.stringWidth(button_label, font, size) + 2 * padding_x
    c.setFillColor(HexColor("#4A4A4A"))
    c.roundRect(button_x, button_y, button_w, button_h, button_h / 2, fill=1, stroke=0)
    write_text(c, button_label, button_x + padding_x, baseline, size, white, font)
    c.linkURL(config["website"]["url"], (button_x, button_y, button_x + button_w,
               button_y + button_h), relative=1, thickness=0)
    label = config["phone"].get("label", "咨询热线")
    display = config["phone"]["display"]
    label_w = pdfmetrics.stringWidth(label, font, size)
    number_w = pdfmetrics.stringWidth(display, font, size)
    phone_x = button_x + button_w + action_gap
    phone_w = label_w + gap + number_w + 2 * padding_x
    c.setStrokeColor(HexColor("#777777"))
    c.setLineWidth(0.8)
    c.roundRect(phone_x, button_y, phone_w, button_h, button_h / 2, fill=0, stroke=1)
    start = phone_x + padding_x
    write_text(c, label, start, baseline, size,
               HexColor(config["style"]["muted"]), font)
    write_text(c, display, start + label_w + gap, baseline, size,
               HexColor(config["style"]["text"]), font)
    c.linkURL(config["phone"]["uri"], (phone_x, button_y, phone_x + phone_w,
               button_y + button_h), relative=1, thickness=0)


def reference_code_label_lines(label, font, size=20, max_width=118):
    # Keep the scan action together and the destination name together.
    lines = label.split("\n")
    if len(lines) == 1 and pdfmetrics.stringWidth(label, font, size) > max_width:
        action = re.match(r"^(扫码(?:添加|进入|访问))(.+)$", label)
        if action:
            lines = [action[1], action[2]]
        else:
            lines, line = [], ""
            tokens = re.findall(r"[A-Za-z0-9]+(?:[./_-][A-Za-z0-9]+)*|\s+|.", label)
            for token in tokens:
                if line and pdfmetrics.stringWidth(line + token, font, size) > max_width:
                    lines.append(line.rstrip())
                    line = ""
                line += token if line else token.lstrip()
            if line:
                lines.append(line.rstrip())
    if (len(lines) > 2 or any(not line or pdfmetrics.stringWidth(line, font, size) > max_width
                              for line in lines)):
        raise ValueError("QR label must fit two lines at size 20; shorten it or add a semantic newline")
    return lines


def draw_reference_codes(c, config, font):
    items = [config["qr"]["wecom"], config["qr"]["mini_program"]]
    if config["qr"]["website"].get("enabled"):
        items.append(dict(config["qr"]["website"], website=True))
    side, gap, y = 94, 28, 68
    first_x = 1376 - side * len(items) - gap * (len(items) - 1)
    muted = HexColor(config["style"]["muted"])
    for index, item in enumerate(items):
        x = first_x + (side + gap) * index
        cx = x + side / 2
        if item.get("website"):
            draw_website_code(c, config["website"]["url"], x, y, side)
        elif item.get("image"):
            draw_original_code(c, item["image"], x, y, side)
        else:
            c.setStrokeColor(HexColor(config["style"]["line"]))
            c.setLineWidth(1)
            c.roundRect(x + 0.5, y + 0.5, side - 1, side - 1, 4, fill=0, stroke=1)
            placeholder = item.get("placeholder", "码图待替换")
            lines = placeholder.split("\n") if "\n" in placeholder else (
                [placeholder[:-3], "待替换"] if placeholder.endswith("待替换") else [placeholder])
            for j, label in enumerate(lines):
                baseline = y + side / 2 + (len(lines) - 1) * 10 - 5 - 20 * j
                write_text(c, label, cx, baseline, 13.5, muted, font, side - 14, align="center")
        label_lines = reference_code_label_lines(item["label"], font)
        for j, label in enumerate(label_lines):
            write_text(c, label, cx, 40 - 24 * j, 20, muted, font, align="center")
        url = config["website"]["url"] if item.get("website") else item.get("link")
        if url:
            c.linkURL(url, (cx - 59, 8, cx + 59, y + side + 3), relative=1, thickness=0)


def make_footer(width, height, config, fonts):
    layout = config["style"].get("layout", "grouped")
    if layout == "grouped":
        return make_grouped_footer(width, height, config, fonts)
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(width, height), pageCompression=1)
    style = config["style"]
    c.setFillColor(HexColor(style["background"]))
    c.rect(0, 0, width, height, fill=1, stroke=0)
    c.setStrokeColor(HexColor(style["line"]))
    c.setLineWidth(0.6)
    c.line(0, height - 0.3, width, height - 0.3)
    scale = min(width / 1440, height / 180)
    c.translate((width - 1440 * scale) / 2, (height - 180 * scale) / 2)
    c.scale(scale, scale)
    font, _ = fonts
    draw_reference_logo(c, config, fonts)
    if layout == "reference-sentence":
        draw_reference_statement(c, config["statement_spans"], font, HexColor(style["text"]),
                                 style.get("endorsement_decoration") == "silver-laurel")
    else:
        draw_reference_metrics(c, config, font)
    draw_reference_actions(c, config, font)
    draw_reference_codes(c, config, font)
    c.showPage()
    c.save()
    out.seek(0)
    return PdfReader(out).pages[0]


def page_transform(page, footer_height):
    crop = tuple(float(x) for x in page.cropbox)
    cx, cy, right, top = crop
    cw, ch = right - cx, top - cy
    if cw <= 0 or ch <= 0:
        raise ValueError("Page CropBox has no visible area")
    rotation = int(page.rotation) % 360
    if rotation == 0:
        matrix = (1, 0, 0, 1, -cx, footer_height - cy)
    elif rotation == 90:
        matrix = (0, -1, 1, 0, -cy, right + footer_height)
    elif rotation == 180:
        matrix = (-1, 0, 0, -1, right, top + footer_height)
    elif rotation == 270:
        matrix = (0, 1, -1, 0, top, footer_height - cx)
    else:
        raise ValueError(f"Unsupported page rotation {rotation}; expected multiples of 90")
    width, height = (ch, cw) if rotation in (90, 270) else (cw, ch)
    return {"ctm": Transformation(matrix), "crop": crop, "rotation": rotation,
            "width": width, "height": height, "footer": footer_height}


def transform_rectangle(rect, op):
    x1, y1, x2, y2 = map(float, rect)
    points = [op.apply_on((x, y)) for x, y in ((x1, y1), (x1, y2), (x2, y1), (x2, y2))]
    return RectangleObject((min(p[0] for p in points), min(p[1] for p in points),
                            max(p[0] for p in points), max(p[1] for p in points)))


def transform_annotations(page, op):
    for ref in page.get("/Annots", []):
        annot = ref.get_object()
        if "/Rect" in annot:
            annot[NameObject("/Rect")] = transform_rectangle(annot["/Rect"], op)
        for key in ("/QuadPoints", "/Vertices", "/L", "/CL"):
            if key in annot:
                coords = list(annot[key])
                if len(coords) % 2:
                    raise ValueError(f"Invalid annotation {key} coordinate count")
                mapped = []
                for i in range(0, len(coords), 2):
                    mapped.extend(FloatObject(v) for v in op.apply_on((coords[i], coords[i + 1])))
                annot[NameObject(key)] = ArrayObject(mapped)
        if "/InkList" in annot:
            strokes = ArrayObject()
            for stroke in annot["/InkList"]:
                mapped = []
                for i in range(0, len(stroke), 2):
                    mapped.extend(FloatObject(v) for v in op.apply_on((stroke[i], stroke[i + 1])))
                strokes.append(ArrayObject(mapped))
            annot[NameObject("/InkList")] = strokes


def transform_destinations(writer, mappings):
    """Update local explicit destinations, including outlines and named destinations."""
    page_by_ref = {p.indirect_reference.idnum: m for p, m in zip(writer.pages, mappings)}
    visited = set()
    transformed_destinations = set()

    def destination(dest):
        if not isinstance(dest, ArrayObject) or len(dest) < 2:
            return
        if id(dest) in transformed_destinations:
            return
        target = dest[0]
        target_id = getattr(target, "idnum", None)
        if target_id is None:
            target_id = getattr(getattr(target, "indirect_reference", None), "idnum", None)
        info = page_by_ref.get(target_id)
        if info is None:
            return
        transformed_destinations.add(id(dest))
        op, crop, rot = info["ctm"], info["crop"], info["rotation"]
        kind = str(dest[1])
        if kind == "/XYZ" and len(dest) >= 5:
            # PDF null means "retain the viewer's current coordinate". Under a
            # quarter-turn that coordinate belongs to the opposite output axis.
            x, y = dest[2], dest[3]
            a, b, c, d, e, f = op.ctm

            def axis_coordinate(value, coefficient, offset):
                return NullObject() if isinstance(value, NullObject) else FloatObject(float(value) * coefficient + offset)

            dest[2] = axis_coordinate(x, a, e) if a else axis_coordinate(y, c, e)
            dest[3] = axis_coordinate(x, b, f) if b else axis_coordinate(y, d, f)
        elif kind == "/FitR" and len(dest) >= 6:
            rect = transform_rectangle(dest[2:6], op)
            dest[2:6] = list(rect)
        elif kind in ("/FitH", "/FitBH", "/FitV", "/FitBV") and len(dest) >= 3:
            horizontal = kind in ("/FitH", "/FitBH")
            if rot in (90, 270):
                dest[1] = NameObject({"/FitH": "/FitV", "/FitBH": "/FitBV",
                                      "/FitV": "/FitH", "/FitBV": "/FitBH"}[kind])
            if not isinstance(dest[2], NullObject):
                val = float(dest[2])
                px, py = op.apply_on((crop[0], val) if horizontal else (val, crop[3]))
                remains_horizontal = horizontal != (rot in (90, 270))
                dest[2] = FloatObject(py if remains_horizontal else px)

    def walk(obj):
        if hasattr(obj, "get_object"):
            obj = obj.get_object()
        if not isinstance(obj, (dict, list)) or id(obj) in visited:
            return
        visited.add(id(obj))
        if isinstance(obj, dict):
            if "/Dest" in obj:
                destination(obj["/Dest"])
            if obj.get("/S") == "/GoTo" and "/D" in obj:
                destination(obj["/D"])
            # Named-destination dictionary entries use /D without /S.
            if "/D" in obj and "/S" not in obj:
                destination(obj["/D"])
            for key, value in obj.items():
                if key not in ("/Parent", "/P", "/Contents", "/Resources"):
                    walk(value)
        else:
            if len(obj) >= 2 and str(obj[1]) in ("/XYZ", "/FitR", "/FitH", "/FitV", "/FitBH", "/FitBV", "/Fit", "/FitB"):
                destination(obj)
                return
            for item in obj:
                walk(item)

    walk(writer.root_object)


def ensure_supported(reader):
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF is not supported; provide an unlocked copy")
    root = reader.trailer["/Root"]
    if root.get("/Perms"):
        raise ValueError("PDF has permission/signature constraints; source remains unchanged")
    form = root.get("/AcroForm")
    if form and form.get_object().get("/Fields"):
        raise ValueError("PDF contains interactive fields or signatures; source remains unchanged")
    for page in reader.pages:
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            if annotation.get("/Subtype") == "/Widget" or annotation.get("/FT") == "/Sig":
                raise ValueError("PDF contains interactive widgets or signatures; source remains unchanged")


def process_pdf(source, target, config, fonts):
    if source.resolve() == target.resolve():
        raise ValueError("Output must differ from source")
    if target.exists():
        raise ValueError(f"Refusing to overwrite existing output: {target}")
    reader = PdfReader(str(source))
    ensure_supported(reader)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    mappings = []
    for page in writer.pages:
        rot = int(page.rotation) % 360
        page_width = float(page.cropbox.height if rot in (90, 270) else page.cropbox.width)
        footer = float(config["style"].get("height_pt") or page_width * config["style"]["height_ratio"])
        mappings.append(page_transform(page, footer))
    transform_destinations(writer, mappings)
    for page, info in zip(writer.pages, mappings):
        op = info["ctm"]
        content = page.get_contents()
        if content is not None:
            cx, cy, right, top = info["crop"]
            content.operations = [([], b"q"),
                                  ([FloatObject(x) for x in (cx, cy, right - cx, top - cy)], b"re"),
                                  ([], b"W"), ([], b"n")] + content.operations + [([], b"Q")]
            page.replace_contents(content)
        page.add_transformation(op)
        transform_annotations(page, op)
        page.rotation = 0
        size = (0, 0, info["width"], info["height"] + info["footer"])
        for box in ("/MediaBox", "/CropBox", "/BleedBox", "/TrimBox", "/ArtBox"):
            if box in page or box in ("/MediaBox", "/CropBox"):
                page[NameObject(box)] = RectangleObject(size)
        page.merge_page(make_footer(info["width"], info["footer"], config, fonts))
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".footer-", suffix=".pdf", dir=str(target.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            writer.write(stream)
        check = PdfReader(temporary)
        if len(check.pages) != len(reader.pages):
            raise ValueError("Output page count changed unexpectedly")
        for page in check.pages:
            uris = set()
            for ref in page.get("/Annots", []):
                action = ref.get_object().get("/A", {})
                if hasattr(action, "get_object"):
                    action = action.get_object()
                uris.add(str(action.get("/URI", "")))
            if config["phone"]["uri"] not in uris or config["website"]["url"] not in uris:
                raise ValueError("Output phone or website link verification failed")
        # Atomic no-clobber publication: hard link refuses an existing target.
        os.link(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"input": str(source), "output": str(target), "status": "ok", "pages": len(reader.pages)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Source PDF or directory")
    parser.add_argument("--output", required=True, help="New PDF or directory")
    parser.add_argument("--config", help="JSON configuration; relative asset paths use its directory")
    parser.add_argument("--report", help="Optional JSON execution report path")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        fonts = register_fonts(config)
        source = Path(args.input).expanduser().resolve()
        output = Path(args.output).expanduser().resolve()
        if source.is_file():
            if source.suffix.lower() != ".pdf" or output.suffix.lower() != ".pdf":
                raise ValueError("Single-file input and output must have .pdf extensions")
            pairs = [(source, output)]
        elif source.is_dir():
            if source == output or source in output.parents:
                raise ValueError("Batch output directory must be outside the source directory")
            if output.exists() and not output.is_dir():
                raise ValueError("Batch output must be a directory")
            pairs = [(p, output / p.relative_to(source).with_name(p.stem + config["output_suffix"] + ".pdf"))
                     for p in sorted(source.rglob("*")) if p.is_file() and p.suffix.lower() == ".pdf"]
            if not pairs:
                raise ValueError("No PDF files found in input directory")
        else:
            raise ValueError(f"Input not found: {source}")
        results = []
        for source_pdf, target in pairs:
            try:
                results.append(process_pdf(source_pdf, target, config, fonts))
            except Exception as exc:
                results.append({"input": str(source_pdf), "output": str(target), "status": "error", "error": str(exc)})
        report = {"files": results,
                  "missing_original_codes": [k for k in ("wecom", "mini_program") if not config["qr"][k].get("image")],
                  "metrics": config["metrics"], "phone_source": config["phone"].get("source")}
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            report_path = Path(args.report).expanduser().resolve()
            if report_path.exists():
                raise ValueError(f"Refusing to overwrite existing report: {report_path}")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("x", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        print(rendered)
        return int(any(r["status"] != "ok" for r in results))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
