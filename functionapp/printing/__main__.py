"""
Command line entry point: python -m printing INPUT.pdf [OUTPUT.pwg] --dpi 300

Exists so the converter can be run without `python -m printing.pwg_converter`,
which warns because importing the package already imports that submodule. Same
arguments, same output, same JSON report.

This is how a raster is produced for a bench test against the real printer,
independent of SharePoint, Graph and the Function App.
"""

from __future__ import annotations

from .pwg_converter import main

if __name__ == "__main__":
    raise SystemExit(main())
