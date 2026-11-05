"""Minimal runtime hook for bundled applications; models use package paths."""
import builtins
import sys

if not hasattr(builtins, "quit"):
    builtins.quit = lambda code=0: sys.exit(code)
