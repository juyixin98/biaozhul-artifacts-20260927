"""Media parsing layer: strict SRT / WebVTT-subset parsing and serialization."""
from .parser import parse_bytes, parse_text
from .writer import render_document

__all__ = ["parse_bytes", "parse_text", "render_document"]
