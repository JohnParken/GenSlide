"""Attachment parsing and editable document/presentation output helpers."""

from pathlib import Path
import re
import unicodedata
from zipfile import ZipFile, BadZipFile

MAX_OUTPUT_CHARS = 100_000
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_DOCX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_SLIDE_BODY_CHARS = 5_000
_SUPPORTED_ATTACHMENTS = {".txt", ".md", ".pdf", ".docx"}


def _normalize(text: str) -> str:
    text = "".join(char for char in text if char in "\t\n" or unicodedata.category(char) != "Cc")
    lines = [line.strip() for line in text.splitlines()]
    cleaned: list[str] = []
    blank_count = 0
    for line in lines:
        if line:
            blank_count = 0
            cleaned.append(line)
        else:
            blank_count += 1
            if blank_count <= 2:
                cleaned.append(line)
    return "\n".join(cleaned).strip()


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_BULLET = re.compile(r"^\s*[-*+•]\s+(.*)$")
_MD_NUMBER = re.compile(r"^\s*\d+[.)、]\s+(.*)$")


def _plain_markdown(text: str) -> str:
    """Strip inline Markdown markers so raw syntax never reaches a delivered file."""
    text = _MD_BOLD.sub(lambda match: match.group(1) or match.group(2), text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", text)
    return text


def _write_markdown_runs(paragraph, text: str) -> None:
    """Write inline text, turning **bold** / __bold__ into real bold runs."""
    position = 0
    for match in _MD_BOLD.finditer(text):
        if match.start() > position:
            paragraph.add_run(_plain_markdown(text[position:match.start()]))
        paragraph.add_run(match.group(1) or match.group(2)).bold = True
        position = match.end()
    if position < len(text):
        paragraph.add_run(_plain_markdown(text[position:]))


def write_markdown_body(document, body: str) -> None:
    """Render a section body into Word headings, list paragraphs and prose paragraphs.

    Blank lines separate paragraphs; list markers become real Word list styles; inline
    bold becomes a bold run. A body without any of these renders as a single paragraph
    whose text is unchanged.
    """
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        heading = _MD_HEADING.match(lines[0])
        if heading:
            document.add_heading(_plain_markdown(heading.group(2)).strip(), level=min(len(heading.group(1)) + 1, 4))
            lines = lines[1:]
            if not lines:
                continue
        if _MD_BULLET.match(lines[0]) or _MD_NUMBER.match(lines[0]):
            for line in lines:
                bullet, number = _MD_BULLET.match(line), _MD_NUMBER.match(line)
                marker = bullet or number
                style = "List Bullet" if bullet else "List Number"
                _write_markdown_runs(document.add_paragraph(style=style), marker.group(1).strip())
            continue
        paragraph = document.add_paragraph()
        for index, line in enumerate(lines):
            if index:
                paragraph.add_run().add_break()
            _write_markdown_runs(paragraph, line)


def parse_attachment(path: Path) -> str:
    """Extract normalized text from a supported attachment within size limits."""
    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_ATTACHMENTS:
        raise ValueError(f"Unsupported attachment type: {suffix or '(none)'}")
    if not path.is_file():
        raise FileNotFoundError(f"Attachment not found: {path}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"Attachment exceeds the {MAX_INPUT_BYTES}-byte input limit")

    if suffix in {".txt", ".md"}:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = path.read_text(encoding="gb18030")
            except UnicodeDecodeError as exc:
                raise ValueError("Text file must be valid UTF-8 or GBK/GB18030") from exc
    elif suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES}-page limit")
        parts = []
        total_chars = 0
        for page in reader.pages:
            extracted = page.extract_text() or ""
            total_chars += len(extracted)
            if total_chars > MAX_OUTPUT_CHARS:
                raise ValueError(f"Extracted text exceeds the {MAX_OUTPUT_CHARS}-character limit")
            if extracted:
                parts.append(extracted)
        text = "\n\n".join(parts)
        if not text.strip():
            raise ValueError("No text could be extracted from the PDF.")
    else:
        from docx import Document

        try:
            with ZipFile(path) as archive:
                entries = archive.infolist()
                if any(entry.flag_bits & 0x1 for entry in entries):
                    raise ValueError("Encrypted DOCX files are not supported")
                expanded_size = sum(entry.file_size for entry in entries)
                if expanded_size > MAX_DOCX_EXPANDED_BYTES:
                    raise ValueError(f"DOCX expanded size exceeds the {MAX_DOCX_EXPANDED_BYTES}-byte limit")
        except BadZipFile as exc:
            raise ValueError("Invalid DOCX archive") from exc
        document = Document(str(path))
        parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        for table in document.tables:
            for row in table.rows:
                parts.extend(cell.text.strip() for cell in row.cells if cell.text.strip())
        text = "\n\n".join(parts)
        if not text.strip():
            raise ValueError("No text content found in the DOCX file.")

    normalized = _normalize(text)
    if len(normalized) > MAX_OUTPUT_CHARS:
        raise ValueError(f"Extracted text exceeds the {MAX_OUTPUT_CHARS}-character limit")
    return normalized


def _validated_content(content: dict) -> tuple[str, list[dict]]:
    if not isinstance(content, dict):
        raise TypeError("content must be a dictionary")
    title = content.get("title")
    sections = content.get("sections")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("content.title must be a non-empty string")
    if not isinstance(sections, list):
        raise ValueError("content.sections must be a list")
    if not sections:
        raise ValueError("content.sections must contain at least one section")
    if len(sections) > 200:
        raise ValueError("content.sections cannot contain more than 200 items")
    total_chars = len(title)
    normalized_sections = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ValueError(f"section {index} must be a dictionary")
        heading, body = section.get("title"), section.get("body")
        notes = section.get("notes", "")
        if not isinstance(heading, str) or not heading.strip():
            raise ValueError(f"section {index} title must be a non-empty string")
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"section {index} body must be a non-empty string")
        if not isinstance(notes, str):
            raise ValueError(f"section {index} notes must be a string")
        total_chars += len(heading) + len(body) + len(notes)
        normalized_sections.append({"title": heading.strip(), "body": body.strip(), "notes": notes.strip()})
    if total_chars > MAX_OUTPUT_CHARS:
        raise ValueError(f"content exceeds the {MAX_OUTPUT_CHARS}-character limit")
    return title.strip(), normalized_sections


def render_content(kind: str, content: dict, directory: Path) -> Path:
    """Render content as an editable DOCX or a presentation with cover and closing slides."""
    if kind not in {"document", "presentation"}:
        raise ValueError("kind must be 'document' or 'presentation'")
    if not isinstance(directory, Path):
        raise TypeError("directory must be a pathlib.Path")
    title, sections = _validated_content(content)
    if kind == "presentation":
        for index, section in enumerate(sections):
            if len(section["body"]) > MAX_SLIDE_BODY_CHARS:
                raise ValueError(f"section {index} body exceeds the {MAX_SLIDE_BODY_CHARS}-character slide limit")
    directory.mkdir(parents=True, exist_ok=True)

    if kind == "document":
        from docx import Document

        output = directory / "document.docx"
        document = Document()
        document.add_heading(title, level=0)
        for section in sections:
            document.add_heading(section["title"], level=1)
            if section["body"]:
                write_markdown_body(document, section["body"])
            # Speaker notes are presentation-only; they must not leak into a Word deliverable.
        document.save(output)
        return output

    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    output = directory / "presentation.pptx"
    presentation = Presentation()
    presentation.slide_width = Inches(10)
    presentation.slide_height = Inches(5.625)
    blank = presentation.slide_layouts[6]

    # Design System Palette - Modern Slate & Electric Blue
    C_DARK_BG = RGBColor(0x0F, 0x17, 0x2A)     # Slate 900 for Cover & Closing
    C_LIGHT_BG = RGBColor(0xF8, 0xFA, 0xFC)    # Slate 50 for Content background
    C_CARD_BG = RGBColor(0xFF, 0xFF, 0xFF)     # Pure White for Card surfaces
    C_CARD_BORDER = RGBColor(0xE2, 0xE8, 0xF0) # Slate 200 for Card borders
    C_ACCENT = RGBColor(0x25, 0x63, 0xEB)      # Blue 600 for Primary Accent
    C_ACCENT_LIGHT = RGBColor(0xDB, 0xEA, 0xFE)# Blue 100 for Badge Surface
    C_ACCENT_TEAL = RGBColor(0x0D, 0x94, 0x88) # Teal 600 for Secondary Accent
    C_TEXT_DARK = RGBColor(0x0F, 0x17, 0x2A)   # Slate 900 for Headings
    C_TEXT_MUTED = RGBColor(0x47, 0x55, 0x69)  # Slate 600 for Body text
    C_TEXT_FAINT = RGBColor(0x94, 0xA3, 0xB8)  # Slate 400 for Meta/Footer
    C_TEXT_WHITE = RGBColor(0xFF, 0xFF, 0xFF)  # White
    C_DIVIDER = RGBColor(0xE2, 0xE8, 0xF0)     # Slate 200 for Dividing lines

    def add_shape(slide, shape_type, x, y, w, h, fill_color, border_color=None, border_width=1):
        shape = slide.shapes.add_shape(shape_type, Inches(x), Inches(y), Inches(w), Inches(h))
        shape.fill.solid()
        shape.fill.fore_color.rgb = fill_color
        if border_color:
            shape.line.color.rgb = border_color
            shape.line.width = Pt(border_width)
        else:
            shape.line.color.rgb = fill_color
            shape.line.width = Pt(0)
        return shape

    def add_textbox(slide, x, y, w, h, margin=0.0):
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(margin)
        tf.margin_right = Inches(margin)
        tf.margin_top = Inches(margin)
        tf.margin_bottom = Inches(margin)
        return box

    def set_para(para, text, size=12, color=C_TEXT_DARK, bold=False, align=None, font_name="Microsoft YaHei"):
        para.text = _plain_markdown(text)
        para.font.size = Pt(size)
        para.font.color.rgb = color
        para.font.bold = bold
        para.font.name = font_name
        if align:
            para.alignment = align

    def parse_body(body_text):
        raw_lines = [l.strip() for l in body_text.split("\n") if l.strip()]
        if not raw_lines:
            return [("", body_text.strip())]
        items = []
        for line in raw_lines:
            cleaned = re.sub(r"^(\d+[\.、\)]|[一二三四五六七八九十]+[\.、\)]|[-*•])\s*", "", line).strip()
            if not cleaned:
                continue
            m = re.match(r"^([^：:\-—]{2,16})[：:\s\-—]\s*(.+)$", cleaned)
            if m:
                items.append((m.group(1).strip(), m.group(2).strip()))
            else:
                items.append(("", cleaned))
        return items or [("", body_text.strip())]

    total_sections = len(sections)

    # 1. 封面页 (Modern Immersive Dark Cover)
    cover = presentation.slides.add_slide(blank)
    cover.background.fill.solid()
    cover.background.fill.fore_color.rgb = C_DARK_BG

    add_shape(cover, MSO_SHAPE.ROUNDED_RECTANGLE, 0.8, 1.0, 1.8, 0.32, C_ACCENT)
    b_tag = add_textbox(cover, 0.8, 1.0, 1.8, 0.32)
    set_para(b_tag.text_frame.paragraphs[0], "PRESENTATION", 10, C_TEXT_WHITE, bold=True, align=PP_ALIGN.CENTER)

    b_title = add_textbox(cover, 0.8, 1.55, 8.4, 1.4)
    set_para(b_title.text_frame.paragraphs[0], title, 32, C_TEXT_WHITE, bold=True)

    add_shape(cover, MSO_SHAPE.RECTANGLE, 0.8, 3.15, 1.2, 0.04, C_ACCENT)

    b_sub = add_textbox(cover, 0.8, 3.35, 8.4, 0.5)
    set_para(b_sub.text_frame.paragraphs[0], "企业级智能演示文稿 · 结构化核心汇报", 14, C_TEXT_FAINT)

    b_meta = add_textbox(cover, 0.8, 4.65, 8.4, 0.35)
    set_para(b_meta.text_frame.paragraphs[0], "GenSlide Executive Delivery · Confidential", 10, RGBColor(0x64, 0x74, 0x8B))

    # 2. 章节正文页 (Adaptive Card Grid & Visual Hierarchy)
    for sec_idx, section in enumerate(sections, 1):
        slide = presentation.slides.add_slide(blank)
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = C_LIGHT_BG

        # Header 导航条
        add_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, 0.8, 0.35, 1.3, 0.24, C_ACCENT_LIGHT)
        b_badge = add_textbox(slide, 0.8, 0.35, 1.3, 0.24)
        set_para(b_badge.text_frame.paragraphs[0], f"SECTION {sec_idx:02d}", 9, C_ACCENT, bold=True, align=PP_ALIGN.CENTER)

        b_sec_t = add_textbox(slide, 0.8, 0.65, 8.4, 0.55)
        set_para(b_sec_t.text_frame.paragraphs[0], section["title"], 22, C_TEXT_DARK, bold=True)

        add_shape(slide, MSO_SHAPE.RECTANGLE, 0.8, 1.25, 8.4, 0.015, C_DIVIDER)

        # 内容卡片排版
        items = parse_body(section["body"])
        n = len(items)

        if n == 1:
            h, d = items[0]
            add_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, 0.8, 1.5, 8.4, 3.2, C_CARD_BG, C_CARD_BORDER)
            add_shape(slide, MSO_SHAPE.RECTANGLE, 0.8, 1.5, 8.4, 0.06, C_ACCENT)
            b_item = add_textbox(slide, 1.1, 1.8, 7.8, 2.6)
            if h:
                set_para(b_item.text_frame.paragraphs[0], h, 18, C_TEXT_DARK, bold=True)
                p1 = b_item.text_frame.add_paragraph()
                set_para(p1, d, 14, C_TEXT_MUTED)
            else:
                set_para(b_item.text_frame.paragraphs[0], d, 15, C_TEXT_MUTED)

        elif n == 2:
            col_w, gap = 4.05, 0.3
            for i, (h, d) in enumerate(items):
                col_x = 0.8 + i * (col_w + gap)
                accent_c = C_ACCENT if i == 0 else C_ACCENT_TEAL
                add_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, col_x, 1.5, col_w, 3.2, C_CARD_BG, C_CARD_BORDER)
                add_shape(slide, MSO_SHAPE.RECTANGLE, col_x, 1.5, col_w, 0.06, accent_c)
                b_num = add_textbox(slide, col_x + 0.3, 1.75, 1.0, 0.45)
                set_para(b_num.text_frame.paragraphs[0], f"{i+1:02d}", 22, accent_c, bold=True)
                b_item = add_textbox(slide, col_x + 0.3, 2.25, 3.45, 2.2)
                if h:
                    set_para(b_item.text_frame.paragraphs[0], h, 16, C_TEXT_DARK, bold=True)
                    p1 = b_item.text_frame.add_paragraph()
                    set_para(p1, d, 13, C_TEXT_MUTED)
                else:
                    set_para(b_item.text_frame.paragraphs[0], d, 14, C_TEXT_MUTED)

        elif n == 3:
            col_w, gap = 2.65, 0.225
            for i, (h, d) in enumerate(items):
                col_x = 0.8 + i * (col_w + gap)
                accent_c = C_ACCENT if i % 2 == 0 else C_ACCENT_TEAL
                add_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, col_x, 1.5, col_w, 3.2, C_CARD_BG, C_CARD_BORDER)
                add_shape(slide, MSO_SHAPE.RECTANGLE, col_x, 1.5, col_w, 0.06, accent_c)
                b_num = add_textbox(slide, col_x + 0.25, 1.75, 0.8, 0.45)
                set_para(b_num.text_frame.paragraphs[0], f"{i+1:02d}", 22, accent_c, bold=True)
                b_item = add_textbox(slide, col_x + 0.25, 2.25, 2.15, 2.2)
                if h:
                    set_para(b_item.text_frame.paragraphs[0], h, 15, C_TEXT_DARK, bold=True)
                    p1 = b_item.text_frame.add_paragraph()
                    set_para(p1, d, 12, C_TEXT_MUTED)
                else:
                    set_para(b_item.text_frame.paragraphs[0], d, 13, C_TEXT_MUTED)

        elif n == 4:
            col_w, gap_x = 4.05, 0.3
            row_h, gap_y = 1.55, 0.18
            for i, (h, d) in enumerate(items):
                r, c = i // 2, i % 2
                card_x = 0.8 + c * (col_w + gap_x)
                card_y = 1.45 + r * (row_h + gap_y)
                accent_c = C_ACCENT if c == 0 else C_ACCENT_TEAL
                add_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, card_x, card_y, col_w, row_h, C_CARD_BG, C_CARD_BORDER)
                add_shape(slide, MSO_SHAPE.RECTANGLE, card_x, card_y, 0.06, row_h, accent_c)
                b_item = add_textbox(slide, card_x + 0.25, card_y + 0.12, 3.65, 1.3)
                p0 = b_item.text_frame.paragraphs[0]
                if h:
                    set_para(p0, f"{i+1:02d}  {h}", 14, C_TEXT_DARK, bold=True)
                    p1 = b_item.text_frame.add_paragraph()
                    set_para(p1, d, 11, C_TEXT_MUTED)
                else:
                    set_para(p0, f"{i+1:02d}  {d}", 12, C_TEXT_MUTED)

        else:
            col_w, gap_x = 4.05, 0.3
            row_h, gap_y = 0.98, 0.12
            for i, (h, d) in enumerate(items[:6]):
                r, c = i // 2, i % 2
                card_x = 0.8 + c * (col_w + gap_x)
                card_y = 1.45 + r * (row_h + gap_y)
                accent_c = C_ACCENT if c == 0 else C_ACCENT_TEAL
                add_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, card_x, card_y, col_w, row_h, C_CARD_BG, C_CARD_BORDER)
                add_shape(slide, MSO_SHAPE.RECTANGLE, card_x, card_y, 0.06, row_h, accent_c)
                b_item = add_textbox(slide, card_x + 0.2, card_y + 0.08, 3.7, 0.82)
                p0 = b_item.text_frame.paragraphs[0]
                if h:
                    set_para(p0, f"{i+1:02d}  {h}", 13, C_TEXT_DARK, bold=True)
                    p1 = b_item.text_frame.add_paragraph()
                    set_para(p1, d, 10.5, C_TEXT_MUTED)
                else:
                    set_para(p0, f"{i+1:02d}  {d}", 11, C_TEXT_MUTED)

        # Footer
        b_foot_l = add_textbox(slide, 0.8, 4.95, 6.0, 0.3)
        set_para(b_foot_l.text_frame.paragraphs[0], title, 9.5, C_TEXT_FAINT)

        b_foot_r = add_textbox(slide, 7.6, 4.95, 1.6, 0.3)
        set_para(b_foot_r.text_frame.paragraphs[0], f"{sec_idx:02d} / {total_sections:02d}", 9.5, C_TEXT_FAINT, align=PP_ALIGN.RIGHT)

        if section.get("notes"):
            slide.notes_slide.notes_text_frame.text = _plain_markdown(section["notes"])

    # 3. 封底页 (Closing Slide)
    closing = presentation.slides.add_slide(blank)
    closing.background.fill.solid()
    closing.background.fill.fore_color.rgb = C_DARK_BG

    add_shape(closing, MSO_SHAPE.ROUNDED_RECTANGLE, 0.8, 1.2, 2.2, 0.3, C_ACCENT)
    b_cbadge = add_textbox(closing, 0.8, 1.2, 2.2, 0.3)
    set_para(b_cbadge.text_frame.paragraphs[0], "END OF PRESENTATION", 9.5, C_TEXT_WHITE, bold=True, align=PP_ALIGN.CENTER)

    b_ty = add_textbox(closing, 0.8, 1.75, 8.4, 1.0)
    set_para(b_ty.text_frame.paragraphs[0], "Thank you", 32, C_TEXT_WHITE, bold=True)

    add_shape(closing, MSO_SHAPE.RECTANGLE, 0.8, 2.9, 1.2, 0.04, C_ACCENT)

    b_ctitle = add_textbox(closing, 0.8, 3.1, 8.4, 0.6)
    set_para(b_ctitle.text_frame.paragraphs[0], title, 16, RGBColor(0xCA, 0xDC, 0xFC))

    b_cqa = add_textbox(closing, 0.8, 3.8, 8.4, 0.5)
    set_para(b_cqa.text_frame.paragraphs[0], "Q & A · 敬请批评指正与深入探讨", 12, C_TEXT_FAINT)

    presentation.save(output)
    return output
