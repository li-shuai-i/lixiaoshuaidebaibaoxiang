"""End-to-end tests for the local HTTP layer.

Each test class boots a real server on an ephemeral port, so we exercise the
same code path the browser uses.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from csvscope.server import serve

CSV = """city,units,revenue
Shanghai,3,120.5
Beijing,7,90.0
Shenzhen,1,45.25
"""


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    # -- helpers ---------------------------------------------------------- #

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as res:
                return res.status, res.read(), res.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read(), err.headers

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def load(self):
        status, body = self.post("/api/load", {"name": "t.csv", "content": CSV})
        self.assertEqual(status, 200)
        return body

    # -- tests ------------------------------------------------------------ #

    def test_health(self):
        status, body, _ = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_index_is_served(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"csvscope", body)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))

    def test_assets_are_served(self):
        for path in ("/assets/style.css", "/assets/app.js"):
            status, body, _ = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertGreater(len(body), 500, path)

    def test_unknown_route_is_404(self):
        status, _, _ = self.get("/nope")
        self.assertEqual(status, 404)

    def test_path_traversal_is_blocked(self):
        status, _, _ = self.get("/assets/../core.py")
        self.assertIn(status, (403, 404))

    def test_load_returns_profiles_and_preview(self):
        body = self.load()
        self.assertEqual(body["row_count"], 3)
        self.assertEqual(body["column_count"], 3)
        self.assertEqual(body["columns"][1]["type"], "int")
        self.assertEqual(len(body["profiles"]), 3)
        self.assertEqual(len(body["preview"]), 3)
        self.assertIn("id", body)

    def test_query_filters_and_sorts(self):
        dataset = self.load()
        status, body = self.post("/api/query", {
            "id": dataset["id"],
            "filters": [{"column": "revenue", "op": "gte", "value": "90"}],
            "sort": {"column": "units", "desc": True},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["rows"][0][0], "Beijing")

    def test_aggregate(self):
        dataset = self.load()
        status, body = self.post("/api/aggregate", {
            "id": dataset["id"],
            "group_by": "city",
            "metric": None,
            "agg": "count",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["series"][0]["value"], 1.0)
        self.assertEqual(body["group_count"], 3)

    def test_histogram(self):
        dataset = self.load()
        status, body = self.post("/api/histogram", {
            "id": dataset["id"], "column": "revenue", "bins": 4,
        })
        self.assertEqual(status, 200)
        self.assertEqual(sum(b["count"] for b in body["bins"]), 3)

    def test_bad_payload_is_400(self):
        status, body = self.post("/api/load", {"name": "empty.csv", "content": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_unknown_dataset_is_400(self):
        status, body = self.post("/api/query", {"id": "does-not-exist"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_example_dataset_is_available(self):
        status, body, _ = self.get("/api/example")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("content", payload)
        self.assertGreater(len(payload["content"]), 100)


if __name__ == "__main__":
    unittest.main()
