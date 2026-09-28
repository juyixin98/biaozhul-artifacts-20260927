"""Render a ParsedDocument back to text. Only the two timestamps per cue may
differ from the input; identifiers, settings, text lines, header and NOTE
blocks are emitted verbatim."""
from app.parsing.timeparse import format_timestamp


def render_document(doc, assignments=None):
    """Render the document, optionally substituting new times per cue index.

    ``assignments``: {cue_index: (start_ms, end_ms)}. Cues not present keep
    their original times. No cue is ever dropped.
    """
    groups = []
    for b in doc.blocks:
        if b.kind in ("header", "note"):
            groups.append("\n".join(b.lines))
            continue
        c = b.cue
        s, e = c.start_ms, c.end_ms
        if assignments and c.index in assignments:
            s, e = assignments[c.index]
        timing = f"{format_timestamp(s, doc.format)} --> {format_timestamp(e, doc.format)}"
        if c.settings:
            timing += " " + c.settings
        lines = []
        if doc.format == "srt":
            lines.append(c.identifier)
        elif c.identifier:
            lines.append(c.identifier)
        lines.append(timing)
        lines.extend(c.text_lines)
        groups.append("\n".join(lines))
    return "\n\n".join(groups) + "\n"
