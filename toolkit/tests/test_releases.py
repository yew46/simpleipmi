from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from exoanchor_toolkit.errors import ToolkitError
from exoanchor_toolkit.firmware import (
    MANIFEST_NAME,
    THIRD_PARTY_NOTICES_NAME,
    create_firmware_archive,
    create_firmware_package,
)
from exoanchor_toolkit.releases import (
    download_firmware_release,
    download_latest_firmware,
    firmware_release_by_tag,
    firmware_releases,
    latest_firmware_release,
    open_firmware_package,
)


OFFSETS = {
    "0x2000": "bootloader/bootloader.bin",
    "0x8000": "partition_table/partition-table.bin",
    "0xf000": "ota_data_initial.bin",
    "0x20000": "si_esphost_esp32p4.bin",
}


class BytesResponse:
    def __init__(self, content: bytes) -> None:
        self.stream = io.BytesIO(content)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        build = self.root / "build"
        build.mkdir()
        for index, relative in enumerate(OFFSETS.values(), start=1):
            path = build / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes([index]) * (1024 + index))
        (build / "flasher_args.json").write_text(
            json.dumps(
                {
                    "flash_files": OFFSETS,
                    "flash_settings": {
                        "flash_mode": "dio",
                        "flash_size": "16MB",
                        "flash_freq": "80m",
                    },
                    "extra_esptool_args": {
                        "after": "hard_reset",
                        "before": "default_reset",
                        "stub": True,
                        "chip": "esp32p4",
                    },
                }
            ),
            encoding="utf-8",
        )
        (build / "project_description.json").write_text(
            json.dumps(
                {
                    "project_version": "0.87.3-dev",
                    "target": "esp32p4",
                }
            ),
            encoding="utf-8",
        )
        (build / "sdkconfig").write_text(
            'CONFIG_SI_BOARD_ID="exoanchor-prototype-v2.3"\n',
            encoding="utf-8",
        )
        (build / THIRD_PARTY_NOTICES_NAME).write_text(
            "Third-party test notices\n",
            encoding="utf-8",
        )
        package = self.root / "package"
        create_firmware_package(build, package)
        archive = create_firmware_archive(
            package,
            self.root
            / "exoanchor-firmware-0.87.3-dev-exoanchor-prototype-v2.3.zip",
        )
        self.archive = archive.read_bytes()
        self.digest = hashlib.sha256(self.archive).hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _release_payload(
        self,
        *,
        prerelease: bool = True,
        digest: str | None = None,
        content: bytes | None = None,
    ) -> list[dict[str, object]]:
        asset_content = self.archive if content is None else content
        return [
            {
                "id": 31,
                "tag_name": "firmware-v0.87.3-dev",
                "name": "P4 IndigoShore v0.87.3-dev",
                "draft": False,
                "prerelease": prerelease,
                "published_at": "2026-07-31T00:00:00Z",
                "html_url": "https://github.com/wxffxx/ExoAnchor/releases/tag/firmware-v0.87.3-dev",
                "assets": [
                    {
                        "id": 41,
                        "name": "exoanchor-firmware-0.87.3-dev-exoanchor-prototype-v2.3.zip",
                        "browser_download_url": "https://github.com/wxffxx/ExoAnchor/releases/download/firmware-v0.87.3-dev/package.zip",
                        "state": "uploaded",
                        "size": len(asset_content),
                        "digest": "sha256:" + (digest or hashlib.sha256(asset_content).hexdigest()),
                    }
                ],
            }
        ]

    @staticmethod
    def _opener(content: bytes):
        def open_response(request, timeout=0):
            return BytesResponse(content)

        return open_response

    def test_latest_release_includes_prereleases_by_default(self) -> None:
        payload = json.dumps(self._release_payload()).encode()
        release = latest_firmware_release(
            opener=self._opener(payload),
        )
        self.assertTrue(release.prerelease)
        self.assertEqual(release.expected_version, "0.87.3-dev")

    def test_release_list_is_sorted_and_can_select_exact_tag(self) -> None:
        payload = self._release_payload()
        older = json.loads(json.dumps(payload[0]))
        older["id"] = 30
        older["tag_name"] = "firmware-v0.87.2"
        older["name"] = "P4 IndigoShore v0.87.2"
        older["prerelease"] = False
        older["published_at"] = "2026-07-30T00:00:00Z"
        older["html_url"] = (
            "https://github.com/wxffxx/ExoAnchor/releases/tag/firmware-v0.87.2"
        )
        older["assets"][0]["id"] = 40
        older["assets"][0]["name"] = (
            "exoanchor-firmware-0.87.2-exoanchor-prototype-v2.3.zip"
        )
        payload.insert(0, older)
        encoded = json.dumps(payload).encode()

        releases = firmware_releases(opener=self._opener(encoded))
        self.assertEqual(
            [release.tag for release in releases],
            ["firmware-v0.87.3-dev", "firmware-v0.87.2"],
        )
        selected = firmware_release_by_tag(
            "wxffxx/ExoAnchor",
            "firmware-v0.87.2",
            opener=self._opener(json.dumps(older).encode()),
        )
        self.assertEqual(selected.name, "P4 IndigoShore v0.87.2")

    def test_unknown_release_tag_is_rejected(self) -> None:
        payload = json.dumps(self._release_payload()[0]).encode()
        with self.assertRaisesRegex(ToolkitError, "is not available"):
            firmware_release_by_tag(
                "wxffxx/ExoAnchor",
                "firmware-v9.9.9",
                opener=self._opener(payload),
            )

    def test_stable_only_rejects_prerelease(self) -> None:
        payload = json.dumps(self._release_payload()).encode()
        with self.assertRaisesRegex(ToolkitError, "no stable"):
            latest_firmware_release(
                include_prerelease=False,
                opener=self._opener(payload),
            )

    def test_download_extract_verify_and_reuse_cache(self) -> None:
        payload = json.dumps(self._release_payload()).encode()
        destination = self.root / "downloads"
        downloaded = download_latest_firmware(
            output_dir=destination,
            api_opener=self._opener(payload),
            download_opener=self._opener(self.archive),
        )
        self.assertFalse(downloaded.cached)
        self.assertEqual(downloaded.package.version, "0.87.3-dev")
        self.assertTrue(downloaded.package.manifest_path.is_file())
        self.assertTrue(
            downloaded.package.manifest_path.is_relative_to(downloaded.directory)
        )
        self.assertTrue((downloaded.directory / MANIFEST_NAME).is_file())

        cached = download_latest_firmware(
            output_dir=destination,
            api_opener=self._opener(payload),
            download_opener=lambda *args, **kwargs: self.fail(
                "cache reuse must not download the asset"
            ),
        )
        self.assertTrue(cached.cached)
        self.assertEqual(cached.directory, downloaded.directory)

    def _download_while_another_publishes(self, destination, on_publish, *, declared_digest=True):
        releases = self._release_payload()
        if not declared_digest:
            del releases[0]["assets"][0]["digest"]
        api_opener = self._opener(json.dumps(releases).encode())

        def outer_asset_opener(request, timeout=0):
            # The first download has already missed the cache. Let a second
            # download publish its complete package before the first continues.
            winner = download_latest_firmware(
                output_dir=destination, api_opener=api_opener,
                download_opener=self._opener(self.archive),
            )
            on_publish(winner)
            return BytesResponse(self.archive)

        return download_latest_firmware(
            output_dir=destination, api_opener=api_opener, download_opener=outer_asset_opener,
        )

    def test_overlapping_downloads_reuse_the_verified_winner(self) -> None:
        for declared_digest in (True, False):
            with self.subTest(declared_digest=declared_digest):
                destination = self.root.resolve() / f"downloads-{declared_digest}"
                published = []
                other = self._download_while_another_publishes(
                    destination, published.append, declared_digest=declared_digest,
                )
                winner, = published
                self.assertFalse(winner.cached)
                self.assertTrue(other.cached)
                self.assertEqual(other.directory, winner.directory)
                self.assertEqual(other.package.manifest_path, winner.package.manifest_path)
                self.assertTrue(other.package.manifest_path.is_file())
                self.assertEqual(winner.archive_sha256, self.digest)
                self.assertEqual(other.archive_sha256, self.digest if declared_digest else "")
                self.assertEqual(list(destination.iterdir()), [winner.directory])

    def test_overlapping_download_rejects_and_preserves_corrupt_winner(self) -> None:
        destination = self.root.resolve() / "downloads"
        published = []

        def corrupt_segment(winner):
            published.append(winner)
            segment = winner.package.segments[0]
            segment.path.write_bytes(b"\xff" * segment.size)

        with self.assertRaisesRegex(ToolkitError, "SHA-256 mismatch"):
            self._download_while_another_publishes(destination, corrupt_segment)
        winner, = published
        segment = winner.package.segments[0]
        self.assertEqual(segment.path.read_bytes(), b"\xff" * segment.size)
        self.assertEqual(list(destination.iterdir()), [winner.directory])

    def test_different_valid_manifest_is_not_reused_after_a_collision(self) -> None:
        destination = self.root.resolve() / "downloads"
        published = []

        def change_board(winner):
            manifest = winner.package.manifest_path
            contents = json.loads(manifest.read_text(encoding="utf-8"))
            contents["firmware"]["board"] = "different-board"
            manifest.write_text(json.dumps(contents), encoding="utf-8")
            published.append(winner)

        with self.assertRaisesRegex(ToolkitError, "does not match downloaded package"):
            self._download_while_another_publishes(destination, change_board)
        winner, = published
        with open_firmware_package(winner.package.manifest_path) as package:
            self.assertEqual(package.version, winner.package.version)
            self.assertEqual(package.board, "different-board")
        self.assertEqual(list(destination.iterdir()), [winner.directory])

    def test_retry_after_truncated_download_publishes_a_clean_package(self) -> None:
        destination = self.root.resolve() / "downloads"
        api_opener = self._opener(json.dumps(self._release_payload()).encode())
        with self.assertRaisesRegex(ToolkitError, "size mismatch"):
            download_latest_firmware(
                output_dir=destination, api_opener=api_opener,
                download_opener=self._opener(self.archive[:-1]),
            )
        self.assertEqual(list(destination.iterdir()), [])
        downloaded = download_latest_firmware(
            output_dir=destination, api_opener=api_opener,
            download_opener=self._opener(self.archive),
        )
        self.assertFalse(downloaded.cached)
        self.assertEqual(downloaded.archive_sha256, self.digest)
        self.assertTrue(downloaded.package.manifest_path.is_file())
        self.assertEqual(list(destination.iterdir()), [downloaded.directory])

    def test_download_can_select_an_exact_release_tag(self) -> None:
        payload = json.dumps(self._release_payload()[0]).encode()
        requested_urls = []

        def api_opener(request, timeout=0):
            requested_urls.append(request.full_url)
            return BytesResponse(payload)

        downloaded = download_firmware_release(
            output_dir=self.root / "selected",
            tag="firmware-v0.87.3-dev",
            api_opener=api_opener,
            download_opener=self._opener(self.archive),
        )
        self.assertEqual(downloaded.release.tag, "firmware-v0.87.3-dev")
        self.assertEqual(downloaded.package.version, "0.87.3-dev")
        self.assertEqual(requested_urls, [
            "https://api.github.com/repos/wxffxx/ExoAnchor/releases/tags/firmware-v0.87.3-dev"
        ])

    def test_release_zip_can_be_opened_directly(self) -> None:
        archive = self.root / "release.zip"
        archive.write_bytes(self.archive)
        with open_firmware_package(archive) as package:
            self.assertEqual(package.version, "0.87.3-dev")
            self.assertEqual(package.board, "exoanchor-prototype-v2.3")

    def test_archive_digest_mismatch_is_rejected(self) -> None:
        payload = json.dumps(
            self._release_payload(digest="0" * 64)
        ).encode()
        with self.assertRaisesRegex(ToolkitError, "SHA-256 mismatch"):
            download_latest_firmware(
                output_dir=self.root / "downloads",
                api_opener=self._opener(payload),
                download_opener=self._opener(self.archive),
            )

    def test_archive_path_traversal_is_rejected(self) -> None:
        malicious_buffer = io.BytesIO()
        with zipfile.ZipFile(malicious_buffer, "w") as archive:
            archive.writestr("../outside", b"bad")
        malicious = malicious_buffer.getvalue()
        payload = json.dumps(
            self._release_payload(content=malicious)
        ).encode()
        with self.assertRaisesRegex(ToolkitError, "unsafe path"):
            download_latest_firmware(
                output_dir=self.root / "downloads",
                api_opener=self._opener(payload),
                download_opener=self._opener(malicious),
            )


if __name__ == "__main__":
    unittest.main()
