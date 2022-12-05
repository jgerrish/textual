"""Provides a simple Label widget."""

from ._static import Static


class Label(Static):
    """A simple label widget for displaying text-oriented renderables."""

    DEFAULT_CSS = """
    Static {
        width: auto;
        height: auto;
    }
    """
    """str: The default styling of a `Label`."""
