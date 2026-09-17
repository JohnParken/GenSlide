"""Attachment parsing and editable document/presentation output helpers."""

from pathlib import Path
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
        text = path.read_text(encoding="utf-8-sig")
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
                document.add_paragraph(section["body"])
            if section["notes"]:
                document.add_paragraph(section["notes"], style="Caption")
        document.save(output)
        return output

    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    output = directory / "presentation.pptx"
    presentation = Presentation()
    presentation.slide_width = Inches(10)
    presentation.slide_height = Inches(5.625)
    blank = presentation.slide_layouts[6]

    def add_text(slide, text: str, x: float, y: float, width: float, height: float,
                 size: int, color: RGBColor, bold: bool = False) -> None:
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(width), Inches(height))
        box.text_frame.word_wrap = True
        for index, line in enumerate(text.split("\n")):
            paragraph = box.text_frame.paragraphs[0] if index == 0 else box.text_frame.add_paragraph()
            run = paragraph.add_run()
            run.text = line
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = color
            run.font.name = "Calibri"

    navy, ice, dark = RGBColor(0x1E, 0x27, 0x61), RGBColor(0xCA, 0xDC, 0xFC), RGBColor(0x1A, 0x1A, 0x2E)
    cover = presentation.slides.add_slide(blank)
    cover.background.fill.solid()
    cover.background.fill.fore_color.rgb = navy
    add_text(cover, title, 0.7, 2, 8.6, 1.2, 32, ice, True)

    for section in sections:
        slide = presentation.slides.add_slide(blank)
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor(0xF4, 0xF6, 0xFF)
        add_text(slide, section["title"], 0.6, 0.35, 8.8, 0.8, 26, navy, True)
        add_text(slide, section["body"], 0.7, 1.4, 8.6, 3.4, 18, dark)
        if section["notes"]:
            slide.notes_slide.notes_text_frame.text = section["notes"]

    closing = presentation.slides.add_slide(blank)
    closing.background.fill.solid()
    closing.background.fill.fore_color.rgb = navy
    add_text(closing, "Thank you", 0.7, 2, 8.6, 0.9, 32, ice, True)
    add_text(closing, title, 0.7, 3.1, 8.6, 0.7, 18, ice)
    presentation.save(output)
    return output
