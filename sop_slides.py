"""One version of an SOP as a PowerPoint deck: a title slide, its purpose, its steps, then owner and version.

No AI or outside service is involved: the slides show what the SOP says. A version that is not approved says so on
every slide, so a copy passed around cannot be taken for the approved procedure.
"""
import io
import math
import os
import re

from flask import current_app

from timeutil import format_date, format_ist

FONT = "Arial"
RED, RED_TINT = "C8102E", "FBEAEC"
INK, MUTED, LINE, TINT, WHITE = "1F2937", "5B6472", "D1D5DB", "F4F5F7", "FFFFFF"
W, H, M = 13.333, 7.5, 0.6
STEP_TOP, STEP_BOTTOM = 1.6, 6.85
LINE_HEIGHT = 0.29
CHARS_PER_LINE = 88  # at 16 pt across the step text's width
MAX_PIECE = 900  # a longer step continues on the next slide
_NUMBERING = re.compile(r"^\s*(?:step\s*\d+\s*[:.)-]?\s*|\d+\s*[.):-]\s*|[-*•]\s+)", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(text):
    """Without the control characters a PowerPoint file cannot hold."""
    return _CONTROL.sub("", text or "")


def _steps(text):
    """The steps as a list, one per line, without the numbers or bullets they were written with."""
    steps = [_NUMBERING.sub("", line).strip() for line in _clean(text).splitlines()]
    return [step for step in steps if step]


def _pieces(step):
    """A step cut at word boundaries, so that even a very long one fits on the slides."""
    pieces = []
    while len(step) > MAX_PIECE:
        cut = step.rfind(" ", 0, MAX_PIECE)
        cut = cut if cut > 0 else MAX_PIECE
        pieces.append(step[:cut])
        step = step[cut:].strip()
    return pieces + [step]


def _pages(steps):
    """[[(number or None, text)]]: the steps divided into slides. Only a step's first piece shows its number."""
    pages, page, used = [], [], 0.0
    room = STEP_BOTTOM - STEP_TOP
    for number, step in enumerate(steps, start=1):
        for i, piece in enumerate(_pieces(step)):
            needed = _height(piece)
            if page and used + needed > room:
                pages.append(page)
                page, used = [], 0.0
            page.append((number if i == 0 else None, piece))
            used += needed
    return pages + [page] if page else pages


def _height(text):
    return max(max(math.ceil(len(text) / CHARS_PER_LINE), 1) * LINE_HEIGHT + 0.16, 0.58)


def _logo():
    """PowerPoint cannot show an SVG, and a hosted copy may have no logo at all."""
    for name in ("logo.png", "logo.jpg"):
        path = os.path.join(current_app.static_folder, name)
        if os.path.exists(path):
            return path
    return None


def build(sop, version):
    """The .pptx file for this version of the SOP, as bytes."""
    # Imported here: only this download needs it, and it would slow every hosted start-up.
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    logo = _logo()
    approved = version.status == "APPROVED" and not sop.retired_at
    state = "Retired" if sop.retired_at else version.status_label
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(W), Inches(H)
    prs.core_properties.title = _clean(f"{sop.code} {sop.title}")
    prs.core_properties.author = _clean(sop.owner.full_name) if sop.owner else ""

    def rgb(value):
        return RGBColor.from_string(value)

    def text(slide, x, y, w, h, paras, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
        """paras: (text, size, bold, colour), with an optional link as a fifth value."""
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        frame = box.text_frame
        frame.word_wrap, frame.vertical_anchor = True, anchor
        frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
        for i, (value, size, bold, colour, *link) in enumerate(paras):
            para = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            para.alignment = align
            run = para.add_run()
            run.text = _clean(value)
            run.font.name, run.font.size, run.font.bold = FONT, Pt(size), bold
            run.font.color.rgb = rgb(colour)
            if link:
                run.hyperlink.address = link[0]
        return box

    def block(slide, x, y, w, h, fill, line=None, kind=MSO_SHAPE.ROUNDED_RECTANGLE):
        shape = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
        if kind == MSO_SHAPE.ROUNDED_RECTANGLE:
            shape.adjustments[0] = 0.06
        shape.fill.solid()
        shape.fill.fore_color.rgb = rgb(fill)
        if line:
            shape.line.color.rgb = rgb(line)
        else:
            shape.line.fill.background()
        shape.shadow.inherit = False
        return shape

    def content_slide(kicker, title, page):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        text(slide, M, 0.4, 10.6, 0.3, [(kicker.upper(), 12, True, RED)])
        text(slide, M, 0.7, 10.6, 0.7, [(title, 26, True, INK)])
        if logo:
            slide.shapes.add_picture(logo, Inches(W - M - 0.62), Inches(0.36), height=Inches(0.72))
        footer = f"{sop.code} · version {version.number}" + ("" if approved else f" · {state.upper()}: NOT THE APPROVED SOP")
        text(slide, M, H - 0.42, 7.5, 0.25, [(footer, 10, not approved, MUTED if approved else RED)])
        text(slide, W - M - 1, H - 0.42, 1, 0.25, [(str(page), 10, False, MUTED)], align=PP_ALIGN.RIGHT)
        return slide

    # Title
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    block(slide, 0, 0, 7.6, H, RED, kind=MSO_SHAPE.RECTANGLE)
    text(slide, 0.7, 1.7, 6.3, 0.5, [(sop.code, 22, False, RED_TINT)])
    text(slide, 0.7, 2.3, 6.3, 2.8, [(sop.title, 38, True, WHITE)])
    if logo:
        slide.shapes.add_picture(logo, Inches(9.5), Inches(1.1), height=Inches(1.9))
    text(slide, 8.2, 3.5, 4.5, 1.8, [(sop.account.name, 18, True, INK), (f"Version {version.number}", 16, False, MUTED)],
         align=PP_ALIGN.CENTER)
    badge = block(slide, 9.2, 4.6, 2.5, 0.5, TINT if approved else RED_TINT, None if approved else RED)
    frame = badge.text_frame
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    frame.paragraphs[0].alignment = PP_ALIGN.CENTER
    run = frame.paragraphs[0].add_run()
    run.text = state
    run.font.name, run.font.size, run.font.bold = FONT, Pt(14), True
    run.font.color.rgb = rgb(INK if approved else RED)

    # Purpose
    page = 2
    slide = content_slide("Purpose", "What this SOP is for", page)
    text(slide, M, 1.7, W - 2 * M, 4.9, [(version.purpose, 20, False, INK)])

    # Steps
    steps = _steps(version.steps)
    pages = _pages(steps)
    for i, items in enumerate(pages, start=1):
        page += 1
        slide = content_slide("Procedure", "Steps" + (f" ({i} of {len(pages)})" if len(pages) > 1 else ""), page)
        y = STEP_TOP
        for number, piece in items:
            if number is not None:
                circle = block(slide, M, y, 0.42, 0.42, RED, kind=MSO_SHAPE.OVAL)
                frame = circle.text_frame
                frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
                frame.vertical_anchor = MSO_ANCHOR.MIDDLE
                frame.paragraphs[0].alignment = PP_ALIGN.CENTER
                run = frame.paragraphs[0].add_run()
                run.text = str(number)
                run.font.name, run.font.size, run.font.bold = FONT, Pt(12), True
                run.font.color.rgb = rgb(WHITE)
            height = _height(piece)
            text(slide, M + 0.6, y + 0.05, W - 2 * M - 0.6, height, [(piece, 16, False, INK)])
            y += height
    if not pages:
        page += 1
        slide = content_slide("Procedure", "Steps", page)
        text(slide, M, 1.7, W - 2 * M, 1, [("The steps are in the SOP document." if version.link else "No steps written yet.", 20, False, MUTED)])

    # Owner and version
    page += 1
    slide = content_slide("Owner and version", "Who owns this SOP", page)
    rows = [("Owner", sop.owner.full_name if sop.owner else "Not assigned"), ("Version", str(version.number)), ("Status", state)]
    if version.approved_at:
        rows.append(("Approved", f"{format_ist(version.approved_at)} by {version.approved_by.full_name}"))
    if sop.review_due and approved:
        rows.append(("Next review", format_date(sop.review_due)))
    if sop.project:
        rows.append(("Project", sop.project.name))
    if version.change_note:
        rows.append(("What changed", version.change_note))
    y = 1.7
    for label, value in rows:
        text(slide, M, y, 2.3, 0.4, [(label, 15, True, MUTED)])
        lines = max(math.ceil(len(value) / 80), 1)
        text(slide, M + 2.4, y, W - 2 * M - 2.4, lines * 0.3, [(value, 15, False, INK)])
        y += max(lines * 0.3, 0.3) + 0.2
    if version.link and y < 6.4:
        text(slide, M, y, 2.3, 0.4, [("Document", 15, True, MUTED)])
        text(slide, M + 2.4, y, W - 2 * M - 2.4, 0.4, [("Open the SOP document", 15, False, RED, version.link)])

    output = io.BytesIO()
    prs.save(output)
    return output.getvalue()


def filename(sop, version):
    title = re.sub(r"[^A-Za-z0-9]+", "-", sop.title).strip("-")[:60]
    return f"{sop.code}-{title or 'SOP'}-v{version.number}.pptx"
