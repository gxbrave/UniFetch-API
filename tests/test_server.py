import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from yunx_downloader.core import ParseError, ParseResult, Platform, Media, MediaKind
from yunx_downloader.server import JobStore, Settings, create_app


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings("test-token-with-at-least-24-characters", Path(self.temp.name))
        self.headers = {"Authorization": "Bearer " + self.settings.token}

    def tearDown(self):
        self.temp.cleanup()

    def test_authorization_job_lifecycle_and_signed_file(self):
        def prepare(result, directory):
            directory.mkdir(parents=True)
            (directory / "001.mp4").write_bytes(b"downloaded-media")
            return [{"kind": "video", "file": "001.mp4", "quality": "4K HEVC"}]

        result = ParseResult(Platform.DOUYIN, "example", "https://www.douyin.com/video/12345678901",
                             medias=[Media("https://example/video.mp4", MediaKind.VIDEO, quality="4K HEVC")])
        with patch("yunx_downloader.server.parse", return_value=result), patch("yunx_downloader.server.prepare_media", side_effect=prepare):
            with TestClient(create_app(self.settings)) as client:
                body = {"text": result.url}
                self.assertEqual(client.post("/v1/jobs", json=body).status_code, 401)
                submitted = client.post("/v1/jobs", json={**body, "token": self.settings.token})
                self.assertEqual(submitted.status_code, 202)
                poll = submitted.json()["poll_url"]
                self.assertEqual(client.get(poll).status_code, 401)
                for _ in range(100):
                    job = client.get(poll, headers=self.headers).json()
                    if job["status"] == "succeeded":
                        break
                    time.sleep(0.01)
                self.assertEqual(job["status"], "succeeded")
                file_url = job["media"][0]["url"]
                self.assertNotIn(self.settings.token, file_url)
                self.assertEqual(client.get(file_url).content, b"downloaded-media")
                self.assertEqual(client.get(file_url).content, b"downloaded-media")
                with self.assertLogs("yunx.api", level="WARNING") as normalized_logs:
                    renamed = client.get(file_url + ".mp4")
                self.assertEqual(renamed.status_code, 200)
                self.assertEqual(renamed.content, b"downloaded-media")
                self.assertEqual(renamed.headers["X-Download-Signature-Normalized"], "file-extension")
                self.assertIn("extension=.mp4", normalized_logs.output[0])
                from urllib.parse import urlsplit, parse_qs, urlencode
                tampered_parts = urlsplit(file_url)
                tampered_query = parse_qs(tampered_parts.query)
                tampered_query["sig"] = ["0" * 64 + ".mp4"]
                with self.assertLogs("yunx.api", level="WARNING"):
                    tampered = client.get(tampered_parts._replace(query=urlencode(tampered_query, doseq=True)).geturl())
                self.assertEqual(tampered.status_code, 401)
                self.assertEqual(client.get(file_url, headers={"Range": "bytes=0-3"}).status_code, 206)
                self.assertEqual(client.get(file_url + "changed").status_code, 401)
                self.assertEqual(client.get(file_url.split("?")[0]).status_code, 401)
                with self.assertLogs("yunx.api", level="WARNING") as logs:
                    missing = client.get(file_url.replace("&sig=", "&amp;sig="))
                    mismatched = client.get(file_url + "changed")
                    from urllib.parse import urlsplit, parse_qs, urlencode
                    parts = urlsplit(file_url)
                    query = parse_qs(parts.query)
                    query["expires"] = ["1"]
                    expired = client.get(parts._replace(query=urlencode(query, doseq=True)).geturl())
                self.assertEqual(missing.headers["X-Download-Error"], "missing_parameters")
                self.assertEqual(mismatched.headers["X-Download-Error"], "signature_mismatch")
                self.assertEqual(expired.headers["X-Download-Error"], "expired")
                self.assertIn("'amp_sig': True", "\n".join(logs.output))
                self.assertNotIn(query["sig"][0], "\n".join(logs.output))
                self.assertNotIn(self.settings.token, "\n".join(logs.output))
                self.assertNotIn("file", job["media"][0])

    def test_rejects_non_platform_host(self):
        with TestClient(create_app(self.settings)) as client:
            response = client.post("/v1/jobs", json={"text": "http://127.0.0.1/douyin.com/video/12345678901"}, headers=self.headers)
            self.assertEqual(response.status_code, 422)

    def test_x_media_failure_retries_public_once_then_ends_job(self):
        primary = ParseResult(Platform.X, "primary", "https://x.com/test/status/2115039599554941098",
                              medias=[Media("https://video.twimg.com/primary.mp4", MediaKind.VIDEO)])
        public = ParseResult(Platform.X, "public", primary.url, _x_public_used=True,
                             medias=[Media("https://video.twimg.com/public.mp4", MediaKind.VIDEO)])
        store = JobStore(self.settings)
        try:
            for retry_succeeds in (True, False):
                identity = ("c" if retry_succeeds else "d") * 32
                with store.connect() as db:
                    db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (identity, "queued", time.time(), time.time(), None, None))
                attempts = []

                def prepare(parsed, directory):
                    attempts.append(parsed)
                    if len(attempts) == 1:
                        directory.mkdir()
                        (directory / "partial.mp4").write_bytes(b"partial")
                        raise ParseError("download failed", "network")
                    self.assertFalse((directory / "partial.mp4").exists())
                    if not retry_succeeds:
                        raise ParseError("backup failed", "network")
                    directory.mkdir()
                    (directory / "001.mp4").write_bytes(b"public media")
                    return [{"kind": "video", "file": "001.mp4", "quality": "1080P"}]

                with self.subTest(retry_succeeds=retry_succeeds), \
                        patch("yunx_downloader.server.parse", return_value=primary), \
                        patch("yunx_downloader.server.prepare_media", side_effect=prepare), \
                        patch("yunx_downloader.server._x_public_retry", return_value=public) as fallback:
                    store.run(identity, primary.url)
                    fallback.assert_called_once_with(primary)
                    self.assertEqual(attempts, [primary, public])
                    job = store.get(identity)
                    self.assertEqual(job["status"], "succeeded" if retry_succeeds else "failed")
        finally:
            store.close()

    def test_proxy_urls_are_resolved_on_each_poll_and_cover_all_media(self):
        identity = "c" * 32
        store = JobStore(self.settings)
        payload = {"media": [{"kind": "live_photo", "file": "001.jpg", "motion_file": "001.mp4", "motion_photo_file": "001.motion.jpg"}]}
        with store.connect() as db:
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (identity, "succeeded", time.time(), time.time(), json.dumps(payload), None))
        store.close()
        with TestClient(create_app(self.settings)) as client:
            for headers, expected in [
                ({"Host": "192.0.2.10:8099"}, "http://192.0.2.10:8099"),
                ({"X-Forwarded-Host": "a.example.com", "X-Forwarded-Proto": "https"}, "https://a.example.com"),
                ({"Forwarded": 'host="b.example.com:8443";proto=https', "X-Forwarded-Prefix": "/media/"}, "https://b.example.com:8443/media"),
            ]:
                with self.subTest(headers=headers):
                    response = client.get(f"/v1/jobs/{identity}", headers={**self.headers, **headers})
                    self.assertEqual(response.status_code, 200, response.text)
                    data = response.json()
                    self.assertEqual(data["poll_url"], f"{expected}/v1/jobs/{identity}")
                    self.assertEqual(data["source_url"], f"{expected}/source.tar.gz")
                    for field in ("url", "motion_url", "motion_photo_url"):
                        self.assertTrue(data["media"][0][field].startswith(expected + "/v1/files/"))
            docs = client.get("/docs", headers={"X-Forwarded-Prefix": "/media"})
            self.assertIn("/media/openapi.json", docs.text)
            home = client.get("/", headers={"X-Forwarded-Host": "a.example.com", "X-Forwarded-Proto": "https", "X-Forwarded-Prefix": "/media"})
            self.assertIn('href="https://a.example.com/media/docs"', home.text)
            for path in (f"/media/v1/jobs/{identity}/", f"/v1/jobs/{identity}/"):
                redirect = client.get(path, headers={"X-Forwarded-Host": "a.example.com", "X-Forwarded-Proto": "https", "X-Forwarded-Prefix": "/media"}, follow_redirects=False)
                self.assertEqual(redirect.headers["location"], f"https://a.example.com/media/v1/jobs/{identity}")

    def test_explicit_public_url_override_is_preserved(self):
        self.settings.public_base_url = "https://fixed.example.com/api"
        with TestClient(create_app(self.settings)) as client:
            response = client.get("/", headers={"X-Forwarded-Host": "other.example.com"})
            self.assertIn('href="https://fixed.example.com/api/docs"', response.text)

    def test_restart_recovers_interrupted_job_and_cleanup_removes_expired(self):
        store = JobStore(self.settings)
        with store.connect() as db:
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", ("a" * 32, "running", 0, 0, None, None))
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", ("b" * 32, "succeeded", 0, 0, json.dumps({"media": []}), None))
        store.close()
        restarted = JobStore(self.settings)
        try:
            self.assertEqual(restarted.get("a" * 32)["status"], "failed")
            expired = self.settings.data_dir / ("b" * 32)
            expired.mkdir()
            restarted.cleanup()
            self.assertFalse(expired.exists())
        finally:
            restarted.close()


if __name__ == "__main__":
    unittest.main()
