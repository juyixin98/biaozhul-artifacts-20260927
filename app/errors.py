"""Structured error types shared across parsing, kernel and API layers."""


class SubtitleParseError(Exception):
    """Raised when input bytes/text cannot be parsed as the declared format.

    Always carries a stable machine-readable ``code`` plus optional source
    location so callers can point at the offending line.
    """

    def __init__(self, code, message, *, line_no=None, line_text=None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.line_no = line_no
        self.line_text = line_text
        self.details = details or {}

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "line_no": self.line_no,
            "line_text": self.line_text,
            "details": self.details,
        }
