import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from exoanchor_toolkit.errors import ToolkitError
from exoanchor_toolkit.http_device import NetworkOtaUpdater


class OtaValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "app.bin"
        path.write_bytes(b"firmware")
        self.size = path.stat().st_size
        self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        self.package = SimpleNamespace(
            board="exoanchor-prototype-v2.3",
            segments=[SimpleNamespace(address=0x20000, path=path, sha256=self.sha256)],
        )

    def client(self, *, active=False, offset=0, start_offset=0):
        client = Mock()
        client.get_json.side_effect = [
            {"device": {"board": self.package.board}},
            self.status(active=active, offset=offset),
        ]
        client.post_json.side_effect = lambda path, body: (
            {"ok": True, "session_id": "upload", "offset": start_offset}
            if path.endswith("/start") else {"sha256": self.sha256}
        )
        client.post_binary.return_value = {"ok": True, "offset": self.size}
        return client

    def status(self, *, active=True, offset=0):
        return {"active": active, "session_id": "upload", "offset": offset,
                "size": self.size, "sha256": self.sha256}

    def assert_no_finish(self, client):
        self.assertFalse(any(call.args[0].endswith("/finish") for call in client.post_json.call_args_list))

    def test_boolean_start_and_resume_offsets_are_rejected_before_chunks(self):
        for value in (False, True):
            for active in (False, True):
                with self.subTest(value=value, active=active):
                    client = self.client(active=active, offset=value, start_offset=value)
                    with self.assertRaises(ToolkitError):
                        NetworkOtaUpdater(client).update(self.package, reboot=False)
                    client.post_binary.assert_not_called()
                    self.assert_no_finish(client)

    def test_boolean_chunk_offset_never_advances_or_finishes_upload(self):
        for value in (False, True):
            with self.subTest(value=value):
                client = self.client()
                client.post_binary.side_effect = [
                    {"ok": True, "offset": value}, {"ok": True, "offset": self.size},
                ]
                with self.assertRaisesRegex(ToolkitError, "invalid OTA chunk offset"):
                    NetworkOtaUpdater(client).update(self.package, reboot=False)
                self.assertEqual(client.post_binary.call_count, 1)
                self.assert_no_finish(client)

    def test_boolean_recovery_offset_does_not_resume_after_failed_chunk(self):
        for value in (False, True):
            with self.subTest(value=value):
                client = self.client()
                client.get_json.side_effect = [
                    {"device": {"board": self.package.board}},
                    self.status(active=False), self.status(offset=value),
                ]
                client.post_binary.side_effect = [
                    ToolkitError("chunk response lost"), {"ok": True, "offset": self.size},
                ]
                with self.assertRaisesRegex(ToolkitError, "chunk response lost"):
                    NetworkOtaUpdater(client).update(self.package, reboot=False)
                self.assertEqual(client.post_binary.call_count, 1)
                self.assert_no_finish(client)

    def test_integer_offsets_allow_fresh_and_resumed_uploads(self):
        for active, offset in ((False, 0), (True, 2)):
            with self.subTest(active=active, offset=offset):
                client = self.client(active=active, offset=offset)
                NetworkOtaUpdater(client).update(self.package, reboot=False)
                client.post_binary.assert_called_once_with(
                    f"/api/ota/upload/chunk?session=upload&offset={offset}", b"firmware"[offset:]
                )
                self.assertEqual(client.post_json.call_args.args[0], "/api/ota/upload/finish")


if __name__ == "__main__":
    unittest.main()
