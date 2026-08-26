"""Small, self-contained PPTX generator used by the local platform.

The optional JavaScript Artifact Tool remains supported, but a local install
should not be blocked by a private npm package.  This generator deliberately
keeps the layout bounded and deterministic so generated files can be verified
and downloaded like every other platform artifact.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt


# The native generator is used for ordinary user-authored documents, which
# commonly contain more than eight sections (travel plans, reports, meeting
# notes, etc.).  Keep a generous safety ceiling, but paginate instead of
# rejecting a valid request merely because it is longer than the old 8-slide
# demo layout.  No content is silently truncated; requests beyond this bound
# still fail with a public, actionable message.
MAX_CONTENT_SLIDES = 40
MAX_POINTS_PER_SLIDE = 6
META_HEADING = re.compile(
    r"^(?:视觉(?:元素)?建议|制作建议|设计说明|版式建议|排版建议|配色建议|"
    r"图表建议|图片建议|演讲者备注|讲者备注|备注|visual direction|design notes?|"
    r"production notes?|speaker notes?)(?:\s*[:：—-].*)?\s*$",
    re.IGNORECASE,
)
ACCENT = RGBColor(61, 141, 255)
ACCENT_SOFT = RGBColor(109, 203, 244)
INK = RGBColor(17, 17, 17)
MUTED = RGBColor(89, 97, 110)
RULE = RGBColor(184, 188, 196)
WHITE = RGBColor(255, 255, 255)


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`~]+", "", str(value or ""))).strip()


def _filter_producer_notes(content: str) -> str:
    """Remove authoring directions that are not meant for the audience."""

    output: list[str] = []
    skipping_meta_section = False
    for raw in str(content or "").splitlines():
        trimmed = raw.strip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", trimmed)
        if heading:
            heading_text = _plain(heading.group(2))
            skipping_meta_section = bool(META_HEADING.fullmatch(heading_text))
            if not skipping_meta_section:
                output.append(raw)
            continue
        if skipping_meta_section:
            continue
        without_list_marker = re.sub(
            r"^(?:[-*+]\s+|\d+[.)、]\s*)", "", trimmed
        )
        if META_HEADING.fullmatch(_plain(without_list_marker)):
            continue
        output.append(raw)
    return "\n".join(output)


def _sections(content: str, fallback_title: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    title = "核心内容"
    points: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            text = _plain(" ".join(paragraph))
            if text:
                points.append(text[:180])
            paragraph.clear()

    def flush_section() -> None:
        flush_paragraph()
        if points:
            sections.append((title, list(points)))
            points.clear()

    for raw in _filter_producer_notes(content).splitlines():
        line = raw.strip()
        if not line:
            flush_paragraph()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            next_title = _plain(heading.group(2))[:46]
            if next_title and next_title != _plain(fallback_title):
                flush_section()
                title = next_title
            continue
        item = re.match(r"^(?:[-*+]\s+|\d+[.)、]\s*)(.+)$", line)
        if item:
            flush_paragraph()
            value = _plain(item.group(1))
            if value:
                points.append(value[:180])
            continue
        if "|" in line and not re.match(r"^\|?\s*[-:| ]+\s*\|?$", line):
            flush_paragraph()
            cells = [_plain(cell) for cell in line.strip("|").split("|")]
            value = " · ".join(cell for cell in cells if cell)
            if value:
                points.append(value[:180])
            continue
        paragraph.append(line)
    flush_section()
    if not sections:
        sections = [("核心内容", [_plain(fallback_title)[:180] or "围绕关键内容形成清晰结论"])]
    return sections


def _add_text(slide, text: str, left: float, top: float, width: float, height: float, *, size: int, color=INK, bold=False, align=PP_ALIGN.LEFT) -> None:
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.clear()
    frame.word_wrap = True
    frame.vertical_anchor = MSO_ANCHOR.TOP
    paragraph = frame.paragraphs[0]
    paragraph.alignment = align
    run = paragraph.add_run()
    run.text = str(text or "")
    run.font.name = "Aptos"
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color


def _add_rect(slide, left: float, top: float, width: float, height: float, fill: RGBColor) -> None:
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.fill.background()


def _add_footer(slide, number: int) -> None:
    _add_text(slide, f"{number:02d}", 12.35, 6.86, 0.45, 0.22, size=10, color=MUTED, align=PP_ALIGN.RIGHT)


def _content_groups(items: Iterable[str]) -> list[list[str]]:
    values = list(items)
    groups = [values[index:index + MAX_POINTS_PER_SLIDE] for index in range(0, len(values), MAX_POINTS_PER_SLIDE)]
    return groups or [["暂无可展示内容"]]


def generate_pptx(title: str, content: str, output_path: str | Path) -> dict[str, object]:
    """Generate an audience-facing PPTX and return basic generation metadata."""

    title = _plain(title)[:72] or "任务文档"
    sections = _sections(content, title)
    expanded: list[tuple[str, list[str]]] = []
    for section_title, points in sections:
        for group in _content_groups(points):
            expanded.append((section_title, group))
    if len(expanded) > MAX_CONTENT_SLIDES:
        raise ValueError(
            f"PowerPoint 内容超过本地生成器的 {MAX_CONTENT_SLIDES} 页安全上限：共 {len(expanded)} 页。"
            "请减少内容或拆分为多份演示文稿。"
        )

    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)
    blank = presentation.slide_layouts[6]

    cover = presentation.slides.add_slide(blank)
    cover.background.fill.solid()
    cover.background.fill.fore_color.rgb = WHITE
    _add_rect(cover, 0.48, 1.52, 0.11, 2.85, ACCENT)
    _add_text(cover, "演示文稿", 0.48, 0.42, 5.0, 0.35, size=16, color=MUTED, bold=True)
    _add_text(cover, title, 0.88, 1.45, 10.7, 2.0, size=38 if len(title) > 40 else 48, bold=True)
    subtitle = sections[0][1][0] if sections and sections[0][1] else "围绕关键内容形成清晰结论"
    _add_text(cover, subtitle[:120], 0.88, 4.65, 9.6, 0.75, size=17, color=MUTED)
    _add_rect(cover, 0.88, 6.32, 11.95, 0.02, RULE)

    for page, (section_title, points) in enumerate(expanded, start=2):
        slide = presentation.slides.add_slide(blank)
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = WHITE
        _add_text(slide, section_title, 0.48, 0.38, 11.9, 0.7, size=30, bold=True)
        _add_rect(slide, 0.48, 1.30, 12.0, 0.02, RULE)
        columns = min(3, max(1, len(points)))
        gap = 0.42
        width = (12.0 - gap * (columns - 1)) / columns
        for index, point in enumerate(points):
            column = index % columns
            row = index // columns
            left = 0.48 + column * (width + gap)
            top = 2.02 + row * 2.05
            _add_rect(slide, left, top, 0.55, 0.05, ACCENT if index % 2 == 0 else ACCENT_SOFT)
            _add_text(slide, f"{index + 1:02d}", left, top + 0.24, 0.55, 0.3, size=12, color=MUTED, bold=True)
            _add_text(slide, point, left, top + 0.70, width, 1.08, size=17 if len(point) < 90 else 14, color=MUTED)
        _add_footer(slide, page)

    if len(expanded) >= 2:
        summary = presentation.slides.add_slide(blank)
        summary.background.fill.solid()
        summary.background.fill.fore_color.rgb = WHITE
        _add_text(summary, "总结", 0.48, 0.42, 3.0, 0.35, size=16, color=MUTED, bold=True)
        _add_text(summary, "关键结论", 0.48, 1.48, 9.5, 0.9, size=42, bold=True)
        takeaways = [f"{name}：{items[0]}" for name, items in sections[:3] if items]
        _add_text(summary, "\n\n".join(takeaways)[:560], 0.48, 3.05, 10.2, 2.3, size=17, color=MUTED)
        _add_rect(summary, 11.3, 1.5, 1.15, 4.4, ACCENT_SOFT)
        _add_footer(summary, len(presentation.slides))

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(output)
    return {"slideCount": len(presentation.slides), "output": str(output), "generator": "python-pptx"}
