"""RE2 engine adapter: compilation with explicit budgets and error taxonomy."""

from .compiler import CompiledPattern, EngineOptions, compile_pattern
from .scanner import Candidate, scan

__all__ = ["CompiledPattern", "EngineOptions", "compile_pattern", "Candidate", "scan"]
