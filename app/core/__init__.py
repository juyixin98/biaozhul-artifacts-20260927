"""Core parsing kernels (importable without the web layer)."""
from .analyzer import AnalysisReport, analyze
from .diagnostics import Finding

__all__ = ["analyze", "AnalysisReport", "Finding"]
