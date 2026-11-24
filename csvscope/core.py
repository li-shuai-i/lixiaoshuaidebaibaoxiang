"""Analysis engine for csvscope.

Pure standard library on purpose: ``git clone`` and run, no pip install needed.

The engine is deliberately UI-agnostic.  A :class:`Dataset` is built from raw
text, then queried through a small set of well defined operations (filter,
sort, paginate, aggregate, histogram).
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import mean, median, pstdev
from typing import Any, Iterable, Sequence

__version__ = "0.1.0"

#: Cells matching these tokens (case-insensitive) are treated as missing.
NULL_TOKENS = frozenset(
    {"", "-", "--", "na", "n/a", "nan", "null", "none", "nil", "?", "unknown"}
)

_CURRENCY_PREFIX = "$€£¥￥"
_GROUP_SEPARATORS = ",_ "

DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%b %d, %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%Y-%m",
    "%Y/%m",
)

TRUE_TOKENS = frozenset({"true", "yes", "y", "t"})
FALSE_TOKENS = frozenset({"false", "no", "n", "f"})

MAX_COLUMNS = 512
MAX_ROWS = 2_000_000

#: Filter operators supported by :meth:`Dataset.query`.
OPS = (
    "contains",
    "not_contains",
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "empty",
    "not_empty",
)

#: Aggregations supported by :meth:`Dataset.aggregate`.
AGGS = ("count", "sum", "avg", "min", "max", "median", "distinct")


class DataError(ValueError):
    """Raised when input cannot be interpreted as a usable table."""


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def sniff_delimiter(sample: str) -> str:
    """Guess the field delimiter of ``sample``.

    ``csv.Sniffer`` is used first; if it fails we fall back to counting
    candidate delimiters on the first non-empty line.
    """
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        pass

    first_line = next((ln for ln in sample.splitlines() if ln.strip()), "")
    counts = {d: first_line.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _clean_number(raw: str) -> str | None:
    """Normalise a numeric-looking cell; return ``None`` if it is not numeric."""
    text = raw.strip()
    if not text:
        return None
    if text[0] in _CURRENCY_PREFIX:
        text = text[1:]
    text = text.strip()
    # Strip thousands separators only when they group digits in threes.
    if re.fullmatch(r"[+-]?\d{1,3}(?:[, _]\d{3})+(?:\.\d+)?", text):
        text = re.sub(r"[, _]", "", text)
    elif re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        pass
    else:
        return None
    try:
        float(text)
    except ValueError:
        return None
    return text


def _parse_date(raw: str) -> datetime | None:
    text = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _is_null(raw: str) -> bool:
    return raw.strip().lower() in NULL_TOKENS


def _detect_type(values: Sequence[str]) -> str:
    """Infer a column type from sample values.

    A column qualifies only if *every* non-null sample value matches, which
    keeps behaviour predictable (one dirty row means ``string``).
    """
    seen = False
    all_int = all_float = all_bool = all_date = True

    for raw in values:
        if _is_null(raw):
            continue
        seen = True
        text = raw.strip()

        if all_int:
            normalised = _clean_number(text)
            if normalised is None or not re.fullmatch(r"[+-]?\d+", normalised):
                all_int = False
        if all_float and _clean_number(text) is None:
            all_float = False
        if all_bool and text.lower() not in TRUE_TOKENS | FALSE_TOKENS:
            all_bool = False
        if all_date and _parse_date(text) is None:
            all_date = False
        if not (all_int or all_float or all_bool or all_date):
            return "string"

    if not seen:
        return "string"
    if all_int:
        return "int"
    if all_float:
        return "float"
    if all_bool:
        return "bool"
    if all_date:
        return "date"
    return "string"


def parse_value(raw: str, kind: str) -> Any:
    """Convert a raw cell into its typed Python value (``None`` when missing)."""
    if _is_null(raw):
        return None
    text = raw.strip()
    if kind == "int":
        cleaned = _clean_number(text)
        return int(float(cleaned)) if cleaned is not None else None
    if kind == "float":
        cleaned = _clean_number(text)
        return float(cleaned) if cleaned is not None else None
    if kind == "bool":
        return text.lower() in TRUE_TOKENS
    if kind == "date":
        parsed = _parse_date(text)
        return parsed if parsed is not None else None
    return text


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Column:
    name: str
    index: int
    kind: str = "string"
    values: list[str] = field(default_factory=list)

    def typed(self) -> list[Any]:
        return [parse_value(v, self.kind) for v in self.values]

    def as_dict(self) -> dict:
        return {"name": self.name, "index": self.index, "type": self.kind}


@dataclass
class Dataset:
    name: str
    columns: list[Column]
    row_count: int

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_text(cls, text: str, name: str = "dataset") -> "Dataset":
        text = text.lstrip("\ufeff")
        if not text.strip():
            raise DataError("Empty input: nothing to parse.")

        delimiter = sniff_delimiter(text[:65536])
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration as exc:  # pragma: no cover - guarded above
            raise DataError("Empty input: nothing to parse.") from exc

        header = [h.strip().lstrip("\ufeff") for h in header]
        if len(header) > MAX_COLUMNS:
            raise DataError(f"Too many columns (limit {MAX_COLUMNS}).")
        header = _dedupe(header)

        bucket: list[list[str]] = [[] for _ in header]
        rows = 0
        for row in reader:
            if not row or (len(row) == 1 and not row[0].strip()):
                continue
            rows += 1
            if rows > MAX_ROWS:
                raise DataError(f"Too many rows (limit {MAX_ROWS:,}).")
            for i in range(len(header)):
                bucket[i].append(row[i] if i < len(row) else "")

        columns = []
        for i, col_name in enumerate(header):
            values = bucket[i]
            columns.append(
                Column(name=col_name or f"column_{i + 1}", index=i,
                       kind=_detect_type(values[:5000]), values=values)
            )

        if rows == 0:
            raise DataError("No data rows found — is the first line a header?")
        return cls(name=name, columns=columns, row_count=rows)

    # -- introspection ----------------------------------------------------- #

    def column(self, ref: str | int) -> Column:
        """Resolve a column by index or by name."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            idx = int(ref)
            if not 0 <= idx < len(self.columns):
                raise DataError(f"Column index out of range: {ref}")
            return self.columns[idx]
        for col in self.columns:
            if col.name == ref:
                return col
        lowered = str(ref).lower()
        for col in self.columns:
            if col.name.lower() == lowered:
                return col
        raise DataError(f"Unknown column: {ref!r}")

    def rows(self, limit: int | None = None) -> list[list[str]]:
        """Row-major view of the raw data."""
        stop = self.row_count if limit is None else min(limit, self.row_count)
        return [
            [col.values[i] for col in self.columns]
            for i in range(stop)
        ]

    def profile(self, ref: str | int) -> dict:
        """Descriptive statistics for a single column."""
        col = self.column(ref)
        typed = col.typed()
        present = [v for v in typed if v is not None]
        distinct = len({str(v) for v in present})

        info: dict[str, Any] = {
            "name": col.name,
            "index": col.index,
            "type": col.kind,
            "total": len(typed),
            "missing": len(typed) - len(present),
            "distinct": distinct,
            "top_values": [],
            "stats": None,
            "histogram": None,
        }

        counts = Counter(str(v) for v in present)
        info["top_values"] = [
            {"value": value, "count": count}
            for value, count in counts.most_common(8)
        ]

        if present and col.kind in ("int", "float"):
            nums = [float(v) for v in present]
            info["stats"] = {
                "min": min(nums),
                "max": max(nums),
                "mean": mean(nums),
                "median": median(nums),
                "stdev": pstdev(nums) if len(nums) > 1 else 0.0,
                "sum": sum(nums),
            }
            # Discrete columns read better with one bar per distinct value.
            bins = 12 if distinct > 12 else max(2, distinct)
            info["histogram"] = _histogram(nums, bins=bins)
        elif present and col.kind == "date":
            stamps = sorted(v for v in present if isinstance(v, (date, datetime)))
            if stamps:
                info["stats"] = {
                    "min": stamps[0].isoformat(),
                    "max": stamps[-1].isoformat(),
                    "count": len(stamps),
                }
        return info

    def profiles(self) -> list[dict]:
        return [self.profile(col.index) for col in self.columns]

    def summary(self) -> dict:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "column_count": len(self.columns),
            "columns": [col.as_dict() for col in self.columns],
        }

    # -- querying ---------------------------------------------------------- #

    def _matching_indices(self, filters: Iterable[dict] | None) -> list[int]:
        filters = [f for f in (filters or []) if f and f.get("column")]
        if not filters:
            return list(range(self.row_count))

        predicates = []
        for spec in filters:
            col = self.column(spec["column"])
            op = spec.get("op", "contains")
            if op not in OPS:
                raise DataError(f"Unknown filter operator: {op!r}")
            needle = spec.get("value", "")
            predicates.append(_make_predicate(col, op, needle))

        return [
            i
            for i in range(self.row_count)
            if all(pred(i) for pred in predicates)
        ]

    def query(
        self,
        filters: Iterable[dict] | None = None,
        sort: dict | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """Filter, sort and paginate the dataset."""
        page_size = max(1, min(int(page_size or 50), 1000))
        indices = self._matching_indices(filters)

        if sort and sort.get("column") not in (None, ""):
            col = self.column(sort["column"])
            typed = col.typed()
            reverse = bool(sort.get("desc"))

            def key(i: int) -> tuple:
                value = typed[i]
                if value is None:
                    return (1, "")
                if col.kind == "string":
                    return (0, value.casefold())
                return (0, value)

            indices.sort(key=key, reverse=reverse)

        total = len(indices)
        pages = max(1, -(-total // page_size))
        page = max(1, min(int(page or 1), pages))
        start = (page - 1) * page_size
        window = indices[start:start + page_size]

        return {
            "rows": [[col.values[i] for col in self.columns] for i in window],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    def aggregate(
        self,
        group_by: str | int,
        metric: str | int | None = None,
        agg: str = "count",
        limit: int = 20,
        filters: Iterable[dict] | None = None,
    ) -> dict:
        """Group by one column and reduce another."""
        if agg not in AGGS:
            raise DataError(f"Unknown aggregation: {agg!r}")
        group_col = self.column(group_by)
        metric_col = self.column(metric) if metric not in (None, "") else None
        if agg != "count" and metric_col is None:
            raise DataError(f"Aggregation {agg!r} needs a numeric column.")
        if agg != "count" and metric_col is not None and metric_col.kind not in ("int", "float"):
            raise DataError(f"Aggregation {agg!r} needs a numeric column, got {metric_col.kind}.")

        indices = self._matching_indices(filters)
        metric_typed = metric_col.typed() if metric_col is not None else None

        counts: dict[str, int] = {}
        values_by_group: dict[str, list[Any]] = {}
        for i in indices:
            key = group_col.values[i].strip() or "(empty)"
            counts[key] = counts.get(key, 0) + 1
            if metric_typed is not None:
                values_by_group.setdefault(key, []).append(metric_typed[i])

        series = []
        for key, row_count in counts.items():
            if agg == "count":
                value = float(row_count)
            else:
                present = [float(v) for v in values_by_group.get(key, []) if v is not None]
                if not present:
                    value = 0.0
                elif agg == "sum":
                    value = sum(present)
                elif agg == "avg":
                    value = mean(present)
                elif agg == "min":
                    value = min(present)
                elif agg == "max":
                    value = max(present)
                elif agg == "median":
                    value = median(present)
                else:  # distinct
                    value = float(len(set(present)))
            series.append({"key": key, "value": value})

        series.sort(key=lambda item: item["value"], reverse=True)
        return {
            "group_by": group_col.name,
            "metric": metric_col.name if metric_col is not None else None,
            "agg": agg,
            "series": series[: max(1, int(limit or 20))],
            "group_count": len(series),
        }

    def histogram(self, ref: str | int, bins: int = 15,
                  filters: Iterable[dict] | None = None) -> dict:
        col = self.column(ref)
        if col.kind not in ("int", "float"):
            raise DataError(f"Histogram needs a numeric column, got {col.kind}.")
        typed = col.typed()
        indices = self._matching_indices(filters)
        numbers = [float(typed[i]) for i in indices if typed[i] is not None]
        return {
            "column": col.name,
            "bins": _histogram(numbers, bins=max(2, min(int(bins or 15), 100))),
            "count": len(numbers),
        }


def _dedupe(names: Sequence[str]) -> list[str]:
    """Make column names unique (``id``, ``id_2``, ...).

    Empty headers are left empty so :meth:`Dataset.from_text` can give them a
    positional name such as ``column_3``.
    """
    seen: Counter[str] = Counter()
    out: list[str] = []
    for raw in names:
        name = raw.strip()
        if not name:
            out.append("")
            continue
        seen[name] += 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return out


def _make_predicate(col: Column, op: str, needle: Any):
    """Build a ``row_index -> bool`` predicate for one filter spec."""
    typed = col.typed()
    numeric = col.kind in ("int", "float")

    if op in ("empty", "not_empty"):
        want_empty = op == "empty"
        return lambda i: (typed[i] is None) is want_empty

    if op in ("gt", "gte", "lt", "lte") and numeric:
        try:
            threshold = float(str(needle).replace(",", ""))
        except ValueError as exc:
            raise DataError(f"Filter on {col.name!r} needs a number, got {needle!r}") from exc
        ops = {
            "gt": lambda v: v > threshold,
            "gte": lambda v: v >= threshold,
            "lt": lambda v: v < threshold,
            "lte": lambda v: v <= threshold,
        }
        test = ops[op]
        return lambda i: typed[i] is not None and test(typed[i])

    if op in ("gt", "gte", "lt", "lte") and col.kind == "date":
        parsed = _parse_date(str(needle))
        if parsed is None:
            raise DataError(f"Filter on {col.name!r} needs a date, got {needle!r}")
        ops = {
            "gt": lambda v: v > parsed,
            "gte": lambda v: v >= parsed,
            "lt": lambda v: v < parsed,
            "lte": lambda v: v <= parsed,
        }
        test = ops[op]
        return lambda i: typed[i] is not None and test(typed[i])

    target = str(needle).strip().lower()

    if op == "contains":
        return lambda i: target in col.values[i].lower()
    if op == "not_contains":
        return lambda i: target not in col.values[i].lower()
    if op == "eq":
        return lambda i: col.values[i].strip().lower() == target
    if op == "ne":
        return lambda i: col.values[i].strip().lower() != target
    raise DataError(f"Operator {op!r} is not supported for {col.kind} columns.")


def _histogram(numbers: Sequence[float], bins: int = 12) -> list[dict]:
    """Equal-width bins with a small amount of tolerance for float noise."""
    if not numbers:
        return []
    low, high = min(numbers), max(numbers)
    if low == high:
        return [{"start": low, "end": high, "count": len(numbers)}]

    width = (high - low) / bins
    bucket = [0] * bins
    for value in numbers:
        idx = int((value - low) / width)
        if idx >= bins:
            idx = bins - 1
        if idx < 0:
            idx = 0
        bucket[idx] += 1

    return [
        {"start": low + i * width, "end": low + (i + 1) * width, "count": bucket[i]}
        for i in range(bins)
    ]
