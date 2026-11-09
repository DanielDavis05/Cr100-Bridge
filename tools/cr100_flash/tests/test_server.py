"""The package server must restrict source IP and keep served bytes fixed."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from cr100_flash.server import PayloadServer


class ServerTests(unittest.TestCase):
    def test_only_expected_peer_can_read_immutable_payload(self):
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "http.jsonl"
            with PayloadServer(bind_ip="127.0.0.1", allow_ip="127.0.0.2",
                               port=0, log_path=log,
                               payloads={"/firmware.upd": b"signed-payload"}) as server:
                url = f"http://127.0.0.1:{server.port}/firmware.upd"
                with self.assertRaises(HTTPError) as rejection:
                    urlopen(url, timeout=2)
                self.assertEqual(rejection.exception.code, 403)
                with self.assertRaises(ValueError):
                    server.add("/firmware.upd", b"replacement")
            self.assertIn('"event": "denied"', log.read_text())

            with PayloadServer(bind_ip="127.0.0.1", allow_ip="127.0.0.1",
                               port=0, log_path=Path(temporary) / "allowed.jsonl",
                               payloads={"/firmware.upd": b"signed-payload"}) as server:
                self.assertEqual(urlopen(f"http://127.0.0.1:{server.port}/firmware.upd",
                                         timeout=2).read(), b"signed-payload")


if __name__ == "__main__":
    unittest.main()
