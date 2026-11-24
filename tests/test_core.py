"""Unit tests for the analysis engine."""

import unittest

from csvscope.core import DataError, Dataset, sniff_delimiter


CSV = """name,qty,price,active,joined,note
widget,3,19.99,true,2026-01-05,first
gadget,10,"1,234.50",false,2026-02-11,
doohickey,7,$12.00,true,2026-03-02,last one
"""


def make(text=CSV):
    return Dataset.from_text(text, name="test.csv")


class DelimiterTests(unittest.TestCase):
    def test_comma(self):
        self.assertEqual(sniff_delimiter("a,b,c\n1,2,3\n"), ",")

    def test_semicolon(self):
        self.assertEqual(sniff_delimiter("a;b;c\n1;2;3\n"), ";")

    def test_tab(self):
        self.assertEqual(sniff_delimiter("a\tb\tc\n1\t2\t3\n"), "\t")

    def test_pipe(self):
        self.assertEqual(sniff_delimiter("a|b|c\n1|2|3\n"), "|")

    def test_single_column_falls_back_to_comma(self):
        self.assertEqual(sniff_delimiter("name\nx\ny\n"), ",")


class TypeInferenceTests(unittest.TestCase):
    def test_types(self):
        ds = make()
        kinds = {c.name: c.kind for c in ds.columns}
        self.assertEqual(kinds["name"], "string")
        self.assertEqual(kinds["qty"], "int")
        self.assertEqual(kinds["price"], "float")
        self.assertEqual(kinds["active"], "bool")
        self.assertEqual(kinds["joined"], "date")
        self.assertEqual(kinds["note"], "string")

    def test_currency_and_thousands_separators(self):
        ds = make()
        price = ds.column("price").typed()
        self.assertEqual(price, [19.99, 1234.50, 12.0])

    def test_null_tokens_become_none(self):
        text = "a,b\n1,x\nfoo,NA\n3,n/a\n"
        ds = Dataset.from_text(text)
        self.assertEqual(ds.column("a").kind, "string")  # one bad row kills int
        self.assertEqual(ds.column("b").typed(), ["x", None, None])

    def test_one_dirty_cell_keeps_column_as_string(self):
        text = "a\n1\n2\nthree\n"
        self.assertEqual(Dataset.from_text(text).column("a").kind, "string")

    def test_date_column_sorts_chronologically(self):
        text = "d\n2026-12-01\n2026-01-05\n2026-06-30\n"
        ds = Dataset.from_text(text)
        rows = ds.query(sort={"column": "d", "desc": False})["rows"]
        self.assertEqual([r[0] for r in rows], ["2026-01-05", "2026-06-30", "2026-12-01"])

    def test_duplicate_and_missing_headers(self):
        text = "id,id,,value\n1,2,3,4\n"
        ds = Dataset.from_text(text)
        self.assertEqual([c.name for c in ds.columns],
                         ["id", "id_2", "column_3", "value"])

    def test_bom_is_stripped(self):
        ds = Dataset.from_text("\ufeffa,b\n1,2\n")
        self.assertEqual(ds.columns[0].name, "a")

    def test_ragged_rows_are_padded(self):
        ds = Dataset.from_text("a,b,c\n1,2\n3,4,5,6\n")
        self.assertEqual(ds.row_count, 2)
        self.assertEqual(ds.rows()[0], ["1", "2", ""])
        self.assertEqual(ds.rows()[1], ["3", "4", "5"])


class ErrorTests(unittest.TestCase):
    def test_empty_input(self):
        with self.assertRaises(DataError):
            Dataset.from_text("   \n  ")

    def test_header_only(self):
        with self.assertRaises(DataError):
            Dataset.from_text("a,b\n")

    def test_unknown_column(self):
        with self.assertRaises(DataError):
            make().query(filters=[{"column": "nope", "op": "eq", "value": "1"}])

    def test_operator_not_valid_for_type(self):
        with self.assertRaises(DataError):
            make().query(filters=[{"column": "name", "op": "gt", "value": "1"}])

    def test_unknown_operator(self):
        with self.assertRaises(DataError):
            make().query(filters=[{"column": "qty", "op": "like", "value": "1"}])

    def test_aggregate_needs_numeric_metric(self):
        with self.assertRaises(DataError):
            make().aggregate("active", "name", "sum")

    def test_histogram_needs_numeric_column(self):
        with self.assertRaises(DataError):
            make().histogram("name")


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.ds = make()

    def test_contains_filter(self):
        result = self.ds.query(filters=[{"column": "name", "op": "contains", "value": "gad"}])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["rows"][0][0], "gadget")

    def test_case_insensitive_equality(self):
        ds = Dataset.from_text("s\nAlpha\nalpha\nBETA\n")
        result = ds.query(filters=[{"column": "s", "op": "eq", "value": "ALPHA"}])
        self.assertEqual(result["total"], 2)

    def test_numeric_comparison(self):
        result = self.ds.query(filters=[{"column": "qty", "op": "gte", "value": "7"}])
        self.assertEqual(result["total"], 2)

    def test_empty_filter(self):
        result = self.ds.query(filters=[{"column": "note", "op": "empty"}])
        self.assertEqual(result["total"], 1)

    def test_multiple_filters_are_anded(self):
        result = self.ds.query(filters=[
            {"column": "qty", "op": "gt", "value": "2"},
            {"column": "active", "op": "eq", "value": "true"},
        ])
        self.assertEqual(result["total"], 2)

    def test_sort_numeric_ascending(self):
        rows = self.ds.query(sort={"column": "qty"})["rows"]
        self.assertEqual([r[1] for r in rows], ["3", "7", "10"])

    def test_sort_descending(self):
        rows = self.ds.query(sort={"column": "price", "desc": True})["rows"]
        self.assertEqual([r[2] for r in rows], ["1,234.50", "19.99", "$12.00"])

    def test_pagination(self):
        ds = Dataset.from_text("n\n" + "\n".join(str(i) for i in range(120)))
        first = ds.query(page=1, page_size=50)
        last = ds.query(page=3, page_size=50)
        self.assertEqual(first["pages"], 3)
        self.assertEqual(len(first["rows"]), 50)
        self.assertEqual(len(last["rows"]), 20)
        self.assertEqual(last["rows"][0][0], "100")

    def test_page_is_clamped(self):
        result = self.ds.query(page=99, page_size=2)
        self.assertEqual(result["page"], 2)

    def test_filters_combine_with_sort_and_paging(self):
        result = self.ds.query(
            filters=[{"column": "active", "op": "eq", "value": "true"}],
            sort={"column": "qty", "desc": True},
            page=1,
            page_size=1,
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["rows"][0][0], "doohickey")


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.ds = make()

    def test_count(self):
        result = self.ds.aggregate("active", agg="count")
        self.assertEqual(result["series"][0]["value"], 2.0)

    def test_sum(self):
        result = self.ds.aggregate("active", "qty", "sum")
        values = {s["key"]: s["value"] for s in result["series"]}
        self.assertEqual(values["true"], 10.0)   # 3 + 7
        self.assertEqual(values["false"], 10.0)

    def test_avg_median_min_max(self):
        def values(agg):
            series = self.ds.aggregate("active", "qty", agg)["series"]
            return {s["key"]: s["value"] for s in series}

        self.assertEqual(values("avg")["true"], 5.0)      # (3 + 7) / 2
        self.assertEqual(values("min")["true"], 3.0)
        self.assertEqual(values("max")["true"], 7.0)
        self.assertEqual(values("median")["true"], 5.0)
        self.assertEqual(values("avg")["false"], 10.0)

    def test_distinct(self):
        ds = Dataset.from_text("g,v\na,1\na,1\na,2\nb,3\n")
        result = ds.aggregate("g", "v", "distinct")
        values = {s["key"]: s["value"] for s in result["series"]}
        self.assertEqual(values["a"], 2.0)

    def test_series_sorted_descending(self):
        ds = Dataset.from_text("g,v\na,1\nb,5\nc,3\n")
        values = [s["value"] for s in ds.aggregate("g", "v", "sum")["series"]]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_limit(self):
        ds = Dataset.from_text("g\n" + "\n".join(f"k{i}" for i in range(50)))
        result = ds.aggregate("g", agg="count", limit=5)
        self.assertEqual(len(result["series"]), 5)
        self.assertEqual(result["group_count"], 50)

    def test_aggregate_respects_filters(self):
        result = self.ds.aggregate("active", "qty", "sum",
                                   filters=[{"column": "qty", "op": "gt", "value": "5"}])
        values = {s["key"]: s["value"] for s in result["series"]}
        # Only gadget (10) and doohickey (7) survive the filter.
        self.assertEqual(values, {"true": 7.0, "false": 10.0})


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.ds = make()

    def test_stats(self):
        profile = self.ds.profile("qty")
        self.assertEqual(profile["type"], "int")
        self.assertEqual(profile["missing"], 0)
        self.assertEqual(profile["distinct"], 3)
        self.assertEqual(profile["stats"]["sum"], 20.0)
        self.assertAlmostEqual(profile["stats"]["mean"], 20 / 3, places=6)

    def test_missing_count(self):
        self.assertEqual(self.ds.profile("note")["missing"], 1)

    def test_top_values(self):
        ds = Dataset.from_text("c\nx\nx\ny\n")
        top = ds.profile("c")["top_values"]
        self.assertEqual(top[0], {"value": "x", "count": 2})

    def test_histogram_bin_count_tracks_cardinality(self):
        # 3 distinct values -> 3 bins, not the default 12.
        self.assertEqual(len(self.ds.profile("qty")["histogram"]), 3)

    def test_constant_column_histogram(self):
        ds = Dataset.from_text("v\n5\n5\n5\n")
        bins = ds.profile("v")["histogram"]
        self.assertEqual(len(bins), 1)
        self.assertEqual(bins[0]["count"], 3)

    def test_histogram_covers_every_value(self):
        bins = self.ds.histogram("qty", bins=6)["bins"]
        self.assertEqual(sum(b["count"] for b in bins), 3)

    def test_summary(self):
        summary = self.ds.summary()
        self.assertEqual(summary["row_count"], 3)
        self.assertEqual(summary["column_count"], 6)


if __name__ == "__main__":
    unittest.main()
