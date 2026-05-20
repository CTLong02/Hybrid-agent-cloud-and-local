"""Task parser package.

Importing each module triggers parser registration via `register()` calls
at module load time. New format? Add a module here following the same pattern.
"""

from . import csv, excel, markdown, word, yaml_json  # noqa: F401
from .base import parse_file, supported_extensions

__all__ = ["parse_file", "supported_extensions"]
