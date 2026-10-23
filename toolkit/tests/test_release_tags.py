import io
import json
import unittest
import urllib.error
from unittest.mock import Mock

from exoanchor_toolkit.errors import ToolkitError
from exoanchor_toolkit.releases import firmware_release_by_tag


REPOSITORY = "wxffxx/ExoAnchor"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}/releases"
TAG = "firmware-v0.87.0"


def release_payload(**overrides):
    return {
        "id": 1, "tag_name": TAG, "name": "Firmware v0.87.0",
        "draft": False, "prerelease": False,
        "published_at": "2026-01-01T00:00:00Z",
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{TAG}",
        "assets": [{
            "id": 2, "name": "exoanchor-firmware-0.87.0.zip",
            "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/{TAG}/package.zip",
            "state": "uploaded", "size": 100,
        }],
        **overrides,
    }


def opener_for(payload):
    return Mock(side_effect=lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))


class ReleaseTagTests(unittest.TestCase):
    def test_older_tag_is_queried_directly_without_the_recent_release_list(self):
        requested = []
        recent = [release_payload(id=i + 2, tag_name=f"newer-{i}") for i in range(30)]

        def opener(request, timeout=0):
            requested.append(request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
            self.assertEqual(timeout, 20)
            payload = release_payload() if request.full_url == f"{API_ROOT}/tags/{TAG}" else recent
            return io.BytesIO(json.dumps(payload).encode())

        release = firmware_release_by_tag(REPOSITORY, TAG, token="test-token", opener=opener)
        self.assertEqual(release.tag, TAG)
        self.assertEqual(requested, [f"{API_ROOT}/tags/{TAG}"])

    def test_tag_is_encoded_as_one_path_component(self):
        tag = "firmware/v0.87.0+测试#1%2F"
        opener = opener_for(release_payload(tag_name=tag))
        release = firmware_release_by_tag(f" {REPOSITORY} ", f" {tag} ", opener=opener)
        self.assertEqual(release.tag, tag)
        self.assertEqual(opener.call_args.args[0].full_url,
                         f"{API_ROOT}/tags/firmware%2Fv0.87.0%2B%E6%B5%8B%E8%AF%95%231%252F")

    def test_blank_tags_and_invalid_repositories_do_not_make_requests(self):
        for repository, tag in ((REPOSITORY, ""), (REPOSITORY, " \t "),
                                ("", TAG), ("owner", TAG), ("owner/repo/extra", TAG)):
            with self.subTest(repository=repository, tag=tag):
                opener = Mock()
                with self.assertRaises(ToolkitError):
                    firmware_release_by_tag(repository, tag, opener=opener)
                opener.assert_not_called()

    def test_direct_lookup_rejects_unusable_or_mismatched_metadata(self):
        payloads = [
            None, [], [release_payload()], {},
            release_payload(tag_name="another-tag"), release_payload(draft=True),
            release_payload(assets=[]), release_payload(assets=release_payload()["assets"] * 2),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ToolkitError):
                    firmware_release_by_tag(REPOSITORY, TAG, opener=opener_for(payload))
        with self.assertRaisesRegex(ToolkitError, "invalid release metadata"):
            firmware_release_by_tag(REPOSITORY, TAG, opener=lambda *a, **kw: io.BytesIO(b"{broken"))

    def test_direct_lookup_preserves_prerelease_filtering(self):
        payload = release_payload(prerelease=True)
        release = firmware_release_by_tag(REPOSITORY, TAG, opener=opener_for(payload))
        self.assertTrue(release.prerelease)
        with self.assertRaises(ToolkitError):
            firmware_release_by_tag(REPOSITORY, TAG, include_prerelease=False, opener=opener_for(payload))

    def test_not_found_names_the_requested_tag_and_repository(self):
        error = urllib.error.HTTPError(f"{API_ROOT}/tags/{TAG}", 404, "Not Found", {}, io.BytesIO())
        with self.assertRaisesRegex(ToolkitError, f"{TAG}.*is not available.*{REPOSITORY}"):
            firmware_release_by_tag(REPOSITORY, TAG, opener=Mock(side_effect=error))

    def test_rate_limit_and_timeout_errors_are_not_reported_as_missing_tags(self):
        failures = [
            (urllib.error.HTTPError(API_ROOT, 403, "Forbidden", {}, io.BytesIO()), "HTTP 403.*rate limit"),
            (TimeoutError("test timeout"), "cannot query GitHub releases.*test timeout"),
        ]
        for failure, message in failures:
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaisesRegex(ToolkitError, message) as raised:
                    firmware_release_by_tag(REPOSITORY, TAG, opener=Mock(side_effect=failure))
                self.assertIs(raised.exception.__cause__, failure)


if __name__ == "__main__":
    unittest.main()
