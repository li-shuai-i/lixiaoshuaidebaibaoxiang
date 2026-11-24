"""csvscope — a local-first CSV/TSV data explorer.

Run it with ``python -m csvscope``; your files never leave the machine.
"""

from .core import Column, Dataset, DataError, __version__

__all__ = ["Column", "Dataset", "DataError", "__version__"]
