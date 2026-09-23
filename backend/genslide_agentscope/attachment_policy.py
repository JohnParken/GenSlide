"""Shared attachment policy; runtime limits are passed explicitly to parsers."""

SUPPORTED_ATTACHMENT_SUFFIXES = frozenset({".txt", ".md", ".pdf", ".docx"})
DEFAULT_INPUT_BYTES = 20 * 1024 * 1024


class AttachmentTooLarge(ValueError):
    """Input exceeds the authorized per-file size."""


class MaterialsTooLarge(ValueError):
    """Extracted text exceeds the authorized material budget."""
