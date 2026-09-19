"""Vercel entrypoints plus a repository-root compatibility package.

When Python runs from the repository root, this package appears on
``sys.path`` before the installable ``src/api`` package. Extend the package
search path so imports such as ``api.app`` keep working both locally and from
an installed wheel.
"""

from pathlib import Path

_SRC_API = Path(__file__).resolve().parents[1] / "src" / "api"
if _SRC_API.is_dir():
    __path__.append(str(_SRC_API))
