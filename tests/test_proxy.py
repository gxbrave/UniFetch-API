import unittest

from fastapi import HTTPException
from starlette.requests import Request

from yunx_downloader.proxy import public_request_url


class ProxyUrlTest(unittest.TestCase):
    def request(self, headers, *, scheme="http", root_path=""):
        return Request({
            "type": "http", "scheme": scheme, "method": "GET",
            "path": "/v1/jobs/example", "root_path": root_path,
            "query_string": b"", "server": ("backend", 8000),
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        })

    def test_external_origins(self):
        cases = [
            ({"Host": "192.0.2.10:8099"}, "http://192.0.2.10:8099"),
            ({"Host": "api.example.com", "X-Forwarded-Proto": "https"}, "https://api.example.com"),
            ({"Host": "api:8000", "X-Forwarded-Host": "a.example.com", "X-Forwarded-Proto": "https"}, "https://a.example.com"),
            ({"Host": "api:8000", "X-Forwarded-Host": "a.example.com, proxy.internal", "X-Forwarded-Proto": "https, http", "X-Forwarded-Prefix": "/download/, /internal"}, "https://a.example.com/download"),
            ({"Host": "api:8000", "Forwarded": 'for="client,one";host="b.example.com:8443";proto=https, for=proxy;host=internal;proto=http'}, "https://b.example.com:8443"),
            ({"Host": "api:8000", "Forwarded": 'host="b.example.com";proto=https', "X-Forwarded-Host": "ignored", "X-Forwarded-Proto": "http", "X-Forwarded-Port": "8000"}, "https://b.example.com"),
            ({"Host": "api:8000", "X-Forwarded-Host": "a.example.com", "X-Forwarded-Proto": "https", "X-Forwarded-Port": "8443"}, "https://a.example.com:8443"),
            ({"Host": "api:8000", "X-Forwarded-Host": "a.example.com:9443", "X-Forwarded-Proto": "https", "X-Forwarded-Port": "8000"}, "https://a.example.com:9443"),
            ({"Host": "api:8000", "X-Forwarded-Proto": "https", "X-Forwarded-Port": "443"}, "https://api"),
            ({"Host": "api:8000", "Forwarded": 'host="[2001:db8::1]:8443";proto=https', "X-Forwarded-Prefix": "/media"}, "https://[2001:db8::1]:8443/media"),
            ({"Host": "api:8000", "X-Forwarded-Host": "[2001:db8::1]", "X-Forwarded-Port": "8443", "X-Forwarded-Proto": "https"}, "https://[2001:db8::1]:8443"),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(public_request_url(self.request(headers)), expected)

    def test_existing_mount_and_prefix_do_not_duplicate(self):
        request = self.request({"Host": "api.example.com", "X-Forwarded-Prefix": "/media/"}, root_path="/media")
        self.assertEqual(public_request_url(request), "http://api.example.com/media")
        self.assertEqual(public_request_url(self.request({"Host": "api.example.com"}, root_path="/mounted")), "http://api.example.com/mounted")

    def test_proxy_headers_can_be_disabled(self):
        request = self.request({"Host": "backend:8000", "Forwarded": "host=public.example.com;proto=https", "X-Forwarded-Prefix": "/media"})
        self.assertEqual(public_request_url(request, False), "http://backend:8000")

    def test_invalid_external_url_is_rejected(self):
        for headers in [
            {"X-Forwarded-Host": "user@evil.example.com"},
            {"X-Forwarded-Host": "evil.example.com/path"},
            {"X-Forwarded-Host": "evil.example.com:99999"},
            {"X-Forwarded-Proto": "javascript"},
            {"X-Forwarded-Port": "-1"},
            {"X-Forwarded-Prefix": "//evil.example.com"},
            {"X-Forwarded-Prefix": "/a/../b"},
            {"X-Forwarded-Prefix": "/%2e%2e/b"},
            {"X-Forwarded-Prefix": "/media?x=1"},
        ]:
            with self.subTest(headers=headers):
                with self.assertRaises(HTTPException) as error:
                    public_request_url(self.request({"Host": "backend:8000", **headers}))
                self.assertEqual(error.exception.status_code, 400)
