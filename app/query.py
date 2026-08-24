"""The filter language, moved to :mod:`core.query` and re-exported here.

The agent needs the same parser the routers do, and the agent does not import
``app``. Nothing else changed: these are the same objects, and this module
exists so that ``from ..query import occurrences_in_range`` keeps meaning what
it has always meant.
"""

from core.query import (  # noqa: F401
    IS_FLAGS,
    QuerySpec,
    _flag_clause,
    _text_clause,
    occurrences_in_range,
    parse_query,
)

__all__ = [
    "IS_FLAGS",
    "QuerySpec",
    "occurrences_in_range",
    "parse_query",
    "_flag_clause",
    "_text_clause",
]
