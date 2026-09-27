"""Client tests: batched GraphQL text and request throttling."""

import unittest
from unittest import mock

import client
from errors import HttpError


class BatchQueryTest(unittest.TestCase):
    def test_aliases_each_value_with_its_own_variable(self):
        query = client.batch_query(client.QUERIES["question"], 2)
        self.assertTrue(query.startswith("query Batch($v0: String!, $v1: String!) {"))
        self.assertIn("r0: question(titleSlug: $v0) {", query)
        self.assertIn("r1: question(titleSlug: $v1) {", query)
        self.assertNotIn("$slug", query)
        self.assertNotIn("#", query)
        self.assertEqual(query.count("{"), query.count("}"))


class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    def read(self):
        return self.body

    def getheader(self, name):
        return self.headers.get(name)


class FakeConnection:
    def __init__(self, responses):
        self.responses = responses

    def request(self, *args):
        pass

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass


class ThrottleTest(unittest.TestCase):
    def test_429_honors_retry_after_and_slows_down_then_recovers(self):
        api = client.LeetCode("s", "c")
        ok = FakeResponse(200, b'{"data": {"x": 1}}')
        api.conn = FakeConnection([FakeResponse(429, headers={"Retry-After": "7"}), ok])
        sleeps = []
        with mock.patch.object(client.time, "sleep", sleeps.append):
            self.assertEqual(api.query("q"), {"x": 1})
        self.assertIn(7.0, sleeps)
        self.assertAlmostEqual(api.delay, client.MIN_DELAY * 2 * 0.9)

    def test_other_http_errors_raise_immediately(self):
        api = client.LeetCode("s", "c")
        api.conn = FakeConnection([FakeResponse(403)])
        with self.assertRaises(HttpError):
            api.query("q")


if __name__ == "__main__":
    unittest.main()
