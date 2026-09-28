"""Document model. Text lines, identifiers and settings are kept verbatim so a
repaired render never rewrites anything except the two timestamps."""
from dataclasses import dataclass, field


@dataclass
class Cue:
    index: int            # 1-based position among cues, in file order
    identifier: str       # SRT counter text or VTT cue id ("" when absent)
    start_ms: int
    end_ms: int
    text_lines: list      # raw payload lines, never modified
    settings: str = ""    # raw cue settings after the end timestamp


@dataclass
class Block:
    kind: str             # "header" | "note" | "cue"
    lines: list = field(default_factory=list)  # raw lines for header/note
    cue: Cue = None


@dataclass
class ParsedDocument:
    format: str           # "srt" | "vtt"
    blocks: list

    @property
    def cues(self):
        return [b.cue for b in self.blocks if b.kind == "cue"]
