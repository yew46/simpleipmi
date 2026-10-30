import contextlib
import hashlib
import io
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from exoanchor_toolkit import cli
from exoanchor_toolkit.errors import ToolkitError
from exoanchor_toolkit.firmware import MANIFEST_NAME
from exoanchor_toolkit.releases import download_firmware_release, open_firmware_package


def package_archive(compression=zipfile.ZIP_DEFLATED):
    images = {f"image-{address:x}.bin": bytes([index]) * 16
              for index, address in enumerate((0x2000, 0x8000, 0xF000, 0x20000), 1)}
    manifest = {
        "schema_version": 1, "product": "exoanchor-esp32p4",
        "firmware": {"version": "1.0.0", "board": "exoanchor-prototype-v2.3",
                     "chip": "esp32p4", "flash_size": "16MB"},
        "flash": {"mode": "dio", "frequency": "80m"},
        "segments": [{"address": f"0x{address:x}", "file": name, "size": len(content),
                      "sha256": hashlib.sha256(content).hexdigest()}
                     for address, (name, content) in zip((0x2000, 0x8000, 0xF000, 0x20000), images.items())],
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest))
        for name, content in images.items():
            archive.writestr(name, content)
    return stream.getvalue()


def malformed_archive(kind):
    compression = zipfile.ZIP_LZMA if kind == "lzma" else zipfile.ZIP_DEFLATED
    data = bytearray(package_archive(compression))
    central = data.index(b"PK\x01\x02")
    name_length, extra_length = struct.unpack_from("<HH", data, 26)
    content_offset = 30 + name_length + extra_length
    if kind == "encrypted":
        # Set the encrypted flag in both headers without changing ZIP structure.
        for offset in (6, central + 8):
            struct.pack_into("<H", data, offset, 1)
    elif kind == "unsupported":
        for offset in (8, central + 10):
            struct.pack_into("<H", data, offset, 99)
    elif kind == "deflate":
        data[content_offset] = 0x06  # Reserved DEFLATE block type.
    elif kind == "lzma":
        data[content_offset + 4] = 0xFF  # Invalid LZMA properties.
    else:
        raise ValueError(kind)
    return bytes(data)


class ArchiveErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.staging = self.root / "temporary-packages"
        self.staging.mkdir()
        tempdir = patch("exoanchor_toolkit.releases.tempfile.tempdir", str(self.staging))
        tempdir.start()
        self.addCleanup(tempdir.stop)

    def assert_archive_rejected(self, content):
        path = self.root / "malformed.zip"
        path.write_bytes(content)
        with self.assertRaisesRegex(ToolkitError, "cannot extract firmware archive"):
            with open_firmware_package(path):
                self.fail("malformed archive must not expose a firmware package")
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_encrypted_archive_is_a_toolkit_error(self):
        self.assert_archive_rejected(malformed_archive("encrypted"))

    def test_unknown_compression_is_a_toolkit_error(self):
        self.assert_archive_rejected(malformed_archive("unsupported"))

    def test_corrupt_deflate_is_a_toolkit_error(self):
        self.assert_archive_rejected(malformed_archive("deflate"))

    @unittest.skipIf(zipfile.lzma is None, "LZMA is unavailable")
    def test_corrupt_lzma_is_a_toolkit_error(self):
        self.assert_archive_rejected(malformed_archive("lzma"))

    def test_cli_verification_reports_an_error_without_a_traceback(self):
        for kind in ("encrypted", "unsupported", "deflate"):
            with self.subTest(kind=kind):
                path = self.root / f"{kind}.zip"
                path.write_bytes(malformed_archive(kind))
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    result = cli.main(["firmware", "verify", str(path)])
                self.assertEqual(result, 2)
                self.assertIn("error: cannot extract firmware archive", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(list(self.staging.iterdir()), [])

    def test_failed_download_cleans_staging_and_preserves_existing_cache(self):
        destination = self.root / "downloads"
        existing = destination / "existing-package"
        existing.mkdir(parents=True)
        sentinel = existing / "keep.bin"
        sentinel.write_bytes(b"previous verified package")
        for kind in ("encrypted", "unsupported", "deflate"):
            with self.subTest(kind=kind):
                content = malformed_archive(kind)
                release = {
                    "id": 1, "tag_name": "firmware-v1.0.0", "name": "Firmware 1.0.0",
                    "published_at": "2026-10-29T00:00:00Z",
                    "html_url": "https://github.com/example/project/releases/tag/firmware-v1.0.0",
                    "assets": [{"id": 2, "name": "exoanchor-firmware-1.0.0.zip", "state": "uploaded",
                                "size": len(content), "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                                "browser_download_url": "https://github.com/example/project/releases/download/firmware-v1.0.0/package.zip"}],
                }
                with self.assertRaisesRegex(ToolkitError, "cannot extract firmware archive"):
                    download_firmware_release(
                        "example/project", tag="firmware-v1.0.0", output_dir=destination,
                        api_opener=lambda *a, **kw: io.BytesIO(json.dumps(release).encode()),
                        download_opener=lambda *a, **kw: io.BytesIO(content),
                    )
                self.assertEqual(list(destination.iterdir()), [existing])
                self.assertEqual(sentinel.read_bytes(), b"previous verified package")

    def test_valid_archives_still_verify_and_clean_up(self):
        methods = [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED]
        if zipfile.bz2 is not None:
            methods.append(zipfile.ZIP_BZIP2)
        if zipfile.lzma is not None:
            methods.append(zipfile.ZIP_LZMA)
        for compression in methods:
            with self.subTest(compression=compression):
                path = self.root / "valid.zip"
                path.write_bytes(package_archive(compression))
                with open_firmware_package(path) as package:
                    self.assertEqual(package.version, "1.0.0")
                    self.assertEqual(len(package.segments), 4)
                    self.assertTrue(package.manifest_path.is_file())
                self.assertFalse(package.manifest_path.exists())
                self.assertEqual(list(self.staging.iterdir()), [])

    def test_unsafe_path_error_remains_specific(self):
        path = self.root / "unsafe.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("../outside", b"do not extract")
        with self.assertRaisesRegex(ToolkitError, "^unsafe path in firmware archive"):
            with open_firmware_package(path):
                self.fail("unsafe archive must not expose a package")
        self.assertEqual(list(self.staging.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
