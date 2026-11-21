import unittest
from pathlib import Path
from unittest.mock import patch

from yunx_downloader.serve import check_health, container_port
from yunx_downloader.server import Settings


class ServeTest(unittest.TestCase):
    def test_container_port_defaults_for_unset_and_empty_values(self):
        for environment in ({}, {'CONTAINER_PORT': ''}, {'CONTAINER_PORT': '  '}):
            with self.subTest(environment=environment), patch.dict('os.environ', environment, clear=True):
                self.assertEqual(container_port(), 8000)

    def test_health_check_uses_configured_port(self):
        with patch.dict('os.environ', {'CONTAINER_PORT': '8100'}), \
                patch('yunx_downloader.serve.urlopen') as request:
            request.return_value.__enter__.return_value.status = 200
            check_health()
            request.assert_called_once_with('http://127.0.0.1:8100/health', timeout=3)

    def test_invalid_ports_are_rejected(self):
        for value in ('0', '65536', 'abc', '-1'):
            with self.subTest(port=value), patch.dict('os.environ', {'CONTAINER_PORT': value}):
                with self.assertRaises(ValueError):
                    container_port()

    def test_empty_optional_settings_use_defaults(self):
        values = {'API_TOKEN': 'test-only-token-at-least-24-characters', 'DATA_DIR': '',
                  'PUBLIC_BASE_URL': '', 'RETENTION_HOURS': '', 'MAX_PENDING_JOBS': '',
                  'TRUST_PROXY_HEADERS': ''}
        with patch.dict('os.environ', values, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.data_dir, Path('/data'))
        self.assertEqual(settings.retention_hours, 24)
        self.assertEqual(settings.max_pending, 10)
        self.assertTrue(settings.trust_proxy_headers)
