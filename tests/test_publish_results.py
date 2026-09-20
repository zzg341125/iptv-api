import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.publish_results import (
    build_release_metadata,
    prepare_pages_site,
    prepare_release_assets,
)


class PublishResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace = Path(self.temp_dir.name)
        self.output = self.workspace / "output"
        self.output.mkdir()
        self.previous_cwd = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, self.previous_cwd)

    def _write(self, relative_path, content=b"data"):
        path = self.output / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_builds_readable_release_metadata_in_configured_time_zone(self):
        metadata = build_release_metadata(
            "Asia/Shanghai",
            now=datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(
            metadata,
            {
                "tag": "playlist-20260920-103000-utc-plus-0800",
                "title": "Generated playlist · 2026-09-20 10:30:00 (Asia/Shanghai)",
                "generated_at": "2026-09-20T10:30:00+08:00",
            },
        )

    def test_prepares_only_canonical_public_assets_and_checksums(self):
        self._write("custom/user_result.txt", b"Demo,http://example.com\n")
        self._write("custom/user_result.m3u", b"#EXTM3U\n")
        self._write("ipv4/result.txt", b"IPv4,http://example.com\n")
        self._write("epg/epg.gz", b"gzip-data")
        self._write("log/log.log", b"private log")
        self._write("data/channel_results.db", b"private database")

        destination = self.workspace / "release-assets"
        manifest = prepare_release_assets(
            final_file="output/custom/user_result.txt",
            destination=destination,
            generated_at="2026-09-08T00:00:00+00:00",
            repository="owner/repository",
            source_sha="abc123",
            workflow_run_id="42",
            release_tag="playlist-20260920-103000-utc-plus-0800",
        )

        self.assertEqual(
            {path.name for path in destination.iterdir()},
            {
                "result.txt",
                "result.m3u",
                "ipv4.txt",
                "epg.gz",
                "manifest.json",
                "SHA256SUMS.txt",
            },
        )
        self.assertEqual(manifest["repository"], "owner/repository")
        self.assertEqual(manifest["generated_at"], "2026-09-08T00:00:00+00:00")
        self.assertEqual(
            manifest["release_tag"],
            "playlist-20260920-103000-utc-plus-0800",
        )
        manifest_file = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest_file, manifest)
        result_digest = hashlib.sha256((destination / "result.txt").read_bytes()).hexdigest()
        checksums = (destination / "SHA256SUMS.txt").read_text(encoding="utf-8")
        self.assertIn(f"{result_digest}  result.txt", checksums)
        self.assertNotIn("log.log", checksums)
        self.assertNotIn("channel_results.db", checksums)

    def test_rejects_final_file_outside_output_directory(self):
        secret = self.workspace / "secret.txt"
        secret.write_text("secret", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "must be inside"):
            prepare_release_assets(
                final_file=secret,
                destination=self.workspace / "release-assets",
            )

    def test_rejects_empty_or_oversized_final_file(self):
        final_file = self._write("result.txt", b"")
        with self.assertRaisesRegex(ValueError, "is empty"):
            prepare_release_assets(
                final_file=final_file,
                destination=self.workspace / "empty-assets",
            )

        final_file.write_bytes(b"too large")
        with patch("scripts.publish_results.MAX_RELEASE_ASSET_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                prepare_release_assets(
                    final_file=final_file,
                    destination=self.workspace / "large-assets",
                )

    def test_prepares_pages_site_with_pages_links(self):
        assets = self.workspace / "release-assets"
        assets.mkdir()
        asset_contents = {
            "result.txt": b"Demo,http://example.com\n",
            "result.m3u": b"#EXTM3U\n",
            "ipv4.txt": b"IPv4,http://example.com\n",
            "ipv4.m3u": b"#EXTM3U\n",
            "ipv6.txt": b"IPv6,http://example.com\n",
            "ipv6.m3u": b"#EXTM3U\n",
            "epg.gz": b"gzip-data",
            "epg.xml": b"<tv></tv>",
            "SHA256SUMS.txt": b"checksums\n",
        }
        for name, content in asset_contents.items():
            (assets / name).write_bytes(content)
        (assets / "manifest.json").write_text(
            json.dumps({
                "generated_at": "2026-09-20T12:00:00+00:00",
                "repository": "owner/repository",
                "release_tag": "playlist-20260920-200000-utc-plus-0800",
            }),
            encoding="utf-8",
        )

        site = self.workspace / "pages-site"
        result = prepare_pages_site(
            assets_directory=assets,
            destination=site,
            pages_base_url="https://owner.github.io/repository/",
        )

        self.assertEqual(
            {path.name for path in site.iterdir()},
            {
                *asset_contents,
                "manifest.json",
                "favicon.svg",
                "index.html",
                "viewer.html",
            },
        )
        self.assertEqual(result["pages_base_url"], "https://owner.github.io/repository")
        index = (site / "index.html").read_text(encoding="utf-8")
        for name in (*asset_contents, "manifest.json"):
            self.assertIn(f"https://owner.github.io/repository/{name}", index)
        self.assertIn("完整结果 / All networks", index)
        self.assertIn("播放器在线使用请复制下方对应的 Pages 结果地址", index)
        self.assertIn("完整播放列表", index)
        self.assertIn("完整文本列表", index)
        self.assertIn("IPv4 结果 / IPv4 only", index)
        self.assertIn("IPv6 结果 / IPv6 only", index)
        self.assertIn("节目单 / Programme guide", index)
        self.assertIn("发布信息 / Publication details", index)
        self.assertIn("下载保存 / Download &amp; Save", index)
        self.assertNotIn("Download &amp; save", index)
        self.assertIn(
            "https://github.com/owner/repository/releases/tag/playlist-20260920-200000-utc-plus-0800",
            index,
        )
        self.assertNotIn("https://github.com/Guovin/iptv-api/releases", index)
        self.assertIn('class="inline-release-link"', index)
        self.assertIn('aria-label="打开 owner/repository Release">Release</a>', index)
        self.assertNotIn(">owner/repository Release</a>", index)
        self.assertIn("2026-09-20T12:00:00+00:00", index)
        self.assertIn('<link rel="icon" href="favicon.svg" type="image/svg+xml">', index)
        self.assertIn('class="brand-mark" src="favicon.svg"', index)
        self.assertEqual(index.count('class="card-action copy-action"'), 10)
        self.assertEqual(index.count('target="_blank" rel="noopener noreferrer"'), 11)
        self.assertIn(
            'href="https://owner.github.io/repository/viewer.html?file=result.m3u"',
            index,
        )
        self.assertIn(
            'href="https://owner.github.io/repository/epg.gz"',
            index,
        )
        viewer = (site / "viewer.html").read_text(encoding="utf-8")
        self.assertIn('new TextDecoder("utf-8").decode(buffer)', viewer)
        self.assertIn("文件不存在或不支持浏览器预览。\\nThe file", viewer)
        self.assertIn('"result.m3u"', viewer)
        self.assertNotIn('"epg.gz"', viewer)
        self.assertIn('navigator.clipboard.writeText(value)', index)
        self.assertIn('已复制 / Copied', index)
        self.assertIn('role="status" aria-live="polite"', index)
        self.assertIn('.hero-copy [lang="en"] { display: block;', index)
        self.assertIn(
            "grid-template-columns: repeat(2, minmax(0, 1fr))",
            index,
        )
        self.assertIn('@media (max-width: 980px)', index)
        self.assertNotIn("结果已发布 / Results published", index)
        self.assertNotIn("secondary-action", index)
        self.assertNotIn("border: 1px solid rgba(255, 255, 255, 0.42)", index)
        self.assertNotIn("主仓库结果说明 / Upstream results notice", index)
        self.assertNotIn("fonts.googleapis.com", index)
        self.assertNotIn("CDN accelerated links", index)

    def test_pages_site_marks_upstream_results_with_fork_guidance(self):
        assets = self.workspace / "release-assets"
        assets.mkdir()
        (assets / "result.txt").write_text("Demo,http://example.com\n", encoding="utf-8")
        (assets / "manifest.json").write_text(
            json.dumps({
                "repository": "Guovin/iptv-api",
                "release_tag": "playlist-20260920-200100-utc-plus-0800",
            }),
            encoding="utf-8",
        )

        site = self.workspace / "pages-site"
        prepare_pages_site(
            assets_directory=assets,
            destination=site,
            pages_base_url="https://guovin.github.io/iptv-api",
        )

        index = (site / "index.html").read_text(encoding="utf-8")
        self.assertIn("主仓库结果说明 / Upstream results notice", index)
        self.assertIn("主仓库发布的 Pages 链接和 Release 结果仅用于功能测试", index)
        self.assertIn(
            "https://github.com/Guovin/iptv-api/releases/tag/playlist-20260920-200100-utc-plus-0800",
            index,
        )
        self.assertEqual(
            index.count('href="https://github.com/Guovin/iptv-api/fork"'),
            2,
        )
        self.assertIn(">Fork 项目</a>", index)
        self.assertIn(">Fork the project</a>", index)

    def test_pages_site_omits_unavailable_optional_results(self):
        assets = self.workspace / "release-assets"
        assets.mkdir()
        (assets / "result.txt").write_text("Demo,http://example.com\n", encoding="utf-8")

        site = self.workspace / "pages-site"
        prepare_pages_site(
            assets_directory=assets,
            destination=site,
            pages_base_url="https://owner.github.io/repository",
        )

        index = (site / "index.html").read_text(encoding="utf-8")
        self.assertIn("https://owner.github.io/repository/result.txt", index)
        self.assertNotIn("IPv4 结果 / IPv4 only", index)
        self.assertNotIn("IPv6 结果 / IPv6 only", index)
        self.assertNotIn("节目单 / Programme guide", index)
        self.assertNotIn("发布信息 / Publication details", index)

    def test_rejects_invalid_pages_url(self):
        with self.assertRaisesRegex(ValueError, "absolute HTTP"):
            prepare_pages_site(
                assets_directory=self.output,
                destination=self.workspace / "pages-site",
                pages_base_url="owner.github.io/repository",
            )

    def test_rejects_oversized_pages_site(self):
        assets = self.workspace / "release-assets"
        assets.mkdir()
        (assets / "result.txt").write_text("result", encoding="utf-8")

        with patch("scripts.publish_results.MAX_PAGES_SITE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "site size limit"):
                prepare_pages_site(
                    assets_directory=assets,
                    destination=self.workspace / "pages-site",
                    pages_base_url="https://owner.github.io/repository",
                )


class PublishWorkflowTests(unittest.TestCase):
    def test_manual_workflow_publishes_pages_and_release_without_git_push(self):
        workflow = Path(".github/workflows/main.yml").read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("git push", workflow)
        self.assertNotIn("git commit", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("pages: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("build_release_metadata(config.time_zone)", workflow)
        self.assertIn("scripts/publish_results.py", workflow)
        self.assertIn("actions/configure-pages@v5", workflow)
        self.assertIn("actions/upload-pages-artifact@v4", workflow)
        self.assertIn("actions/deploy-pages@v4", workflow)
        self.assertNotIn('release_title="$RELEASE_TITLE (test only)"', workflow)
        self.assertIn("pages_base_url: ${{ steps.generate.outputs.pages_base_url }}", workflow)
        self.assertIn("release_title: ${{ steps.release.outputs.title }}", workflow)
        self.assertIn("PAGES_BASE: ${{ needs.generate.outputs.pages_base_url }}", workflow)
        self.assertIn("RELEASE_TAG: ${{ needs.generate.outputs.release_tag }}", workflow)
        self.assertIn("RELEASE_TITLE: ${{ needs.generate.outputs.release_title }}", workflow)
        self.assertIn(
            "name: playlist-release-assets-${{ steps.release.outputs.tag }}",
            workflow,
        )
        self.assertIn(
            "name: playlist-release-assets-${{ needs.generate.outputs.release_tag }}",
            workflow,
        )
        self.assertNotIn("github.run_attempt", workflow)
        self.assertIn('pages_link="<a href=\\"$PAGES_BASE\\" target=\\"_blank\\"', workflow)
        self.assertIn("[Fork 项目](https://github.com/Guovin/iptv-api/fork)", workflow)
        self.assertNotIn("此预发布版会保留，用于独立统计本次附件的下载量", workflow)
        self.assertNotIn("gh release edit", workflow)
        self.assertNotIn("gh release upload", workflow)
        self.assertNotIn("--clobber", workflow)
        self.assertIn("--generated-at", workflow)
        self.assertIn("--prerelease", workflow)
        self.assertNotIn("gh-pages", workflow)
        self.assertNotIn("--cdn-url", workflow)
        self.assertNotIn("public_base_url", workflow)
        self.assertNotIn("CDN M3U", workflow)

    def test_generated_output_is_ignored_by_git(self):
        ignore_patterns = Path(".gitignore").read_text(encoding="utf-8").splitlines()

        self.assertIn("/output/", ignore_patterns)
