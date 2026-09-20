import argparse
import hashlib
import html
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_RELEASE_ASSET_BYTES = 2 * 1024 * 1024 * 1024 - 1
MAX_PAGES_SITE_BYTES = 1024 * 1024 * 1024

OPTIONAL_ASSETS = {
    "ipv4/result.txt": "ipv4.txt",
    "ipv4/result.m3u": "ipv4.m3u",
    "ipv6/result.txt": "ipv6.txt",
    "ipv6/result.m3u": "ipv6.m3u",
    "epg/epg.xml": "epg.xml",
    "epg/epg.gz": "epg.gz",
}

PAGE_ASSET_GROUPS = (
    {
        "id": "all-results",
        "eyebrow": "结果 / Results",
        "title": "完整结果 / All networks",
        "description": "包含所有可用频道。 / Includes all available channels.",
        "assets": (
            ("result.m3u", "M3U", "完整播放列表", "Complete playlist"),
            ("result.txt", "TXT", "完整文本列表", "Complete text list"),
        ),
    },
    {
        "id": "ipv4-results",
        "eyebrow": "IPv4",
        "title": "IPv4 结果 / IPv4 only",
        "description": "仅包含 IPv4 地址，适用于未启用 IPv6 或 IPv6 连接不稳定的网络。 / Contains IPv4 addresses only, for networks without IPv6 or with unstable IPv6.",
        "assets": (
            ("ipv4.m3u", "M3U", "IPv4 播放列表", "IPv4 playlist"),
            ("ipv4.txt", "TXT", "IPv4 文本列表", "IPv4 text list"),
        ),
    },
    {
        "id": "ipv6-results",
        "eyebrow": "IPv6",
        "title": "IPv6 结果 / IPv6 only",
        "description": "仅包含 IPv6 地址，请在当前网络和播放设备支持 IPv6 时使用。 / Contains IPv6 addresses only; use it when the current network and player support IPv6.",
        "assets": (
            ("ipv6.m3u", "M3U", "IPv6 播放列表", "IPv6 playlist"),
            ("ipv6.txt", "TXT", "IPv6 文本列表", "IPv6 text list"),
        ),
    },
    {
        "id": "epg-results",
        "eyebrow": "EPG",
        "title": "节目单 / Programme guide",
        "description": "为支持 EPG 的播放器提供节目单数据，GZIP 格式体积更小。",
        "assets": (
            ("epg.gz", "GZIP", "EPG 压缩文件", "Compressed EPG"),
            ("epg.xml", "XML", "EPG XML 文件", "EPG XML data"),
        ),
    },
    {
        "id": "verification-files",
        "eyebrow": "校验 / Verify",
        "title": "发布信息 / Publication details",
        "description": "下载并保存结果时，可使用清单和 SHA-256 校验值确认文件完整性。",
        "assets": (
            ("manifest.json", "JSON", "发布清单", "Release manifest"),
            ("SHA256SUMS.txt", "SHA-256", "文件校验值", "File checksums"),
        ),
    },
)

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RELEASE_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_PAGE_FAVICON = Path(__file__).resolve().parent.parent / "static/images/favicon.svg"
PREVIEWABLE_PAGE_ASSETS = {
    "result.m3u",
    "result.txt",
    "ipv4.m3u",
    "ipv4.txt",
    "ipv6.m3u",
    "ipv6.txt",
    "epg.xml",
    "manifest.json",
    "SHA256SUMS.txt",
}


def _normalize_http_url(value, label):
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{label} must be an absolute HTTP(S) URL: {value}")
    return url


def _resolve_output_file(path, output_dir):
    candidate = path if path.is_absolute() else Path.cwd() / path
    output_root = output_dir.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(output_root)
    except ValueError as exc:
        raise ValueError(f"Release input must be inside {output_root}: {path}") from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise ValueError(f"Release input must be a regular file: {path}")
    size = resolved.stat().st_size
    if size <= 0:
        raise ValueError(f"Release input is empty: {path}")
    if size > MAX_RELEASE_ASSET_BYTES:
        raise ValueError(f"Release input exceeds the GitHub release asset limit: {path}")
    return resolved


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release_metadata(time_zone_name, now=None):
    try:
        configured_zone = ZoneInfo(time_zone_name)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"Invalid release time zone: {time_zone_name}") from exc
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    local_time = instant.astimezone(configured_zone).replace(microsecond=0)
    offset = local_time.strftime("%z")
    offset_direction = "plus" if offset.startswith("+") else "minus"
    offset_token = f"utc-{offset_direction}-{offset[1:]}"
    return {
        "tag": f"playlist-{local_time:%Y%m%d-%H%M%S}-{offset_token}",
        "title": f"Generated playlist · {local_time:%Y-%m-%d %H:%M:%S} ({time_zone_name})",
        "generated_at": local_time.isoformat(timespec="seconds"),
    }


def _load_page_metadata(assets_directory):
    manifest_path = assets_directory / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _render_page_sections(pages_base_url, copied_names):
    available_names = set(copied_names)
    sections = []
    copy_icon = (
        '<svg viewBox="0 0 20 20" aria-hidden="true">'
        '<rect x="6.5" y="6.5" width="9" height="9" rx="2"/>'
        '<path d="M4.5 13.5h-1a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1"/></svg>'
    )
    arrow_icon = (
        '<svg viewBox="0 0 20 20" aria-hidden="true">'
        '<path d="M7.5 4.5h8v8M15 5 5 15"/></svg>'
    )
    for group in PAGE_ASSET_GROUPS:
        cards = []
        for name, file_type, label, english_label in group["assets"]:
            if name not in available_names:
                continue
            url = f"{pages_base_url}/{name}"
            escaped_url = html.escape(url, quote=True)
            if name in PREVIEWABLE_PAGE_ASSETS:
                open_url = f"{pages_base_url}/viewer.html?file={quote(name, safe='')}"
                open_label = "预览内容 / Preview"
            else:
                open_url = url
                open_label = "打开文件 / Open"
            escaped_open_url = html.escape(open_url, quote=True)
            cards.append(f"""
        <article class="result-card">
          <span class="file-type">{html.escape(file_type)}</span>
          <span class="result-name">{html.escape(label)}</span>
          <span class="result-name-en">{html.escape(english_label)}</span>
          <code>{escaped_url}</code>
          <span class="result-actions">
            <button class="card-action copy-action" type="button" data-copy-url="{escaped_url}">
              {copy_icon}<span class="action-label">复制链接 / Copy</span>
            </button>
            <a class="card-action open-action" href="{escaped_open_url}" target="_blank" rel="noopener noreferrer">
              <span>{open_label}</span>{arrow_icon}
            </a>
          </span>
        </article>""")
        if not cards:
            continue
        sections.append(f"""
    <section class="result-section" aria-labelledby="{group['id']}">
      <div class="section-heading">
        <p class="eyebrow">{html.escape(group['eyebrow'])}</p>
        <h2 id="{group['id']}">{html.escape(group['title'])}</h2>
        <p>{html.escape(group['description'])}</p>
      </div>
      <div class="result-grid">{''.join(cards)}
      </div>
    </section>""")
    return "".join(sections)


def _render_page_viewer(copied_names):
    previewable_names = sorted(set(copied_names) & PREVIEWABLE_PAGE_ASSETS)
    allowed_json = json.dumps(previewable_names, ensure_ascii=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="theme-color" content="#1d4ed8">
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <title>IPTV-API 文件预览 / File preview</title>
  <style>
    :root {{ color-scheme: light dark; --page: #eff6ff; --surface: #ffffff; --text: #172554; --muted: #475569; --border: #bfdbfe; --primary: #1d4ed8; }}
    * {{ box-sizing: border-box; }}
    body {{ min-height: 100vh; margin: 0; background: var(--page); color: var(--text); font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ width: min(1200px, calc(100% - 32px)); margin: 0 auto; padding: 24px 0 40px; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }}
    .heading {{ min-width: 0; }}
    h1 {{ margin: 0; overflow-wrap: anywhere; font-size: clamp(20px, 3vw, 28px); line-height: 1.25; }}
    .subtitle {{ margin: 2px 0 0; color: var(--muted); font-size: 13px; }}
    a {{ color: var(--primary); font-weight: 700; text-underline-offset: 3px; }}
    a:focus-visible {{ outline: 3px solid var(--primary); outline-offset: 3px; border-radius: 4px; }}
    .actions {{ display: flex; flex: none; flex-wrap: wrap; gap: 12px; }}
    pre {{ min-height: 320px; margin: 0; padding: 20px; overflow: auto; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); box-shadow: 0 10px 30px rgba(30, 64, 175, 0.08); color: var(--text); font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }}
    @media (max-width: 700px) {{ main {{ width: min(100% - 24px, 1200px); padding-top: 16px; }} header {{ align-items: flex-start; flex-direction: column; }} }}
    @media (prefers-color-scheme: dark) {{ :root {{ --page: #081225; --surface: #0f1f3a; --text: #eff6ff; --muted: #cbd5e1; --border: #27446f; --primary: #93c5fd; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="heading">
        <h1 id="file-name">文件预览 / File preview</h1>
        <p class="subtitle">强制使用 UTF-8 解码，仅用于浏览器查看；播放器仍应使用原始 Pages 链接。</p>
      </div>
      <nav class="actions" aria-label="预览操作 / Preview actions">
        <a href="./">返回结果页 / Back</a>
        <a id="raw-link" href="./">打开原始文件 / Raw file</a>
      </nav>
    </header>
    <pre id="content" aria-live="polite">正在加载 / Loading…</pre>
  </main>
  <script>
    const allowedFiles = new Set({allowed_json});
    const fileName = new URLSearchParams(window.location.search).get("file") || "";
    const heading = document.getElementById("file-name");
    const content = document.getElementById("content");
    const rawLink = document.getElementById("raw-link");

    if (!allowedFiles.has(fileName)) {{
      heading.textContent = "无法预览 / Preview unavailable";
      content.textContent = "文件不存在或不支持浏览器预览。\\nThe file is unavailable or cannot be previewed.";
      rawLink.hidden = true;
    }} else {{
      const rawUrl = `./${{encodeURIComponent(fileName)}}`;
      heading.textContent = fileName;
      document.title = `${{fileName}} · IPTV-API`;
      rawLink.href = rawUrl;
      fetch(rawUrl, {{ cache: "no-cache" }})
        .then((response) => {{
          if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
          return response.arrayBuffer();
        }})
        .then((buffer) => {{
          content.textContent = new TextDecoder("utf-8").decode(buffer);
        }})
        .catch(() => {{
          content.textContent = "加载失败，请打开原始文件或稍后重试。\\nLoading failed. Open the raw file or try again later.";
        }});
    }}
  </script>
</body>
</html>
"""


def prepare_release_assets(
        final_file,
        destination,
        output_dir="output",
        generated_at=None,
        repository=None,
        source_sha=None,
        workflow_run_id=None,
        release_tag=None,
):
    output_dir = Path(output_dir)
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    output_dir = output_dir.resolve(strict=True)

    destination = Path(destination)
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Release destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    final_path = _resolve_output_file(Path(final_file), output_dir)
    if final_path.suffix.lower() != ".txt":
        raise ValueError(f"The configured final_file must use the .txt extension: {final_file}")

    sources = [(final_path, "result.txt")]
    final_m3u = final_path.with_suffix(".m3u")
    if final_m3u.exists():
        sources.append((_resolve_output_file(final_m3u, output_dir), "result.m3u"))
    for relative_path, asset_name in OPTIONAL_ASSETS.items():
        candidate = output_dir / relative_path
        if candidate.exists():
            sources.append((_resolve_output_file(candidate, output_dir), asset_name))

    copied = []
    seen_names = set()
    for source, asset_name in sources:
        if asset_name in seen_names:
            raise ValueError(f"Duplicate release asset name: {asset_name}")
        seen_names.add(asset_name)
        target = destination / asset_name
        shutil.copyfile(source, target)
        copied.append(target)

    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    assets = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(copied, key=lambda item: item.name)
    ]
    manifest = {
        "schema_version": 1,
        "generated_at": generated_at,
        "repository": repository or "",
        "source_sha": source_sha or "",
        "workflow_run_id": workflow_run_id or "",
        "release_tag": release_tag or "",
        "assets": assets,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    checksum_paths = [*copied, manifest_path]
    checksum_lines = [
        f"{_sha256(path)}  {path.name}"
        for path in sorted(checksum_paths, key=lambda item: item.name)
    ]
    (destination / "SHA256SUMS.txt").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="utf-8",
    )
    return manifest


def prepare_pages_site(
        assets_directory,
        destination,
        pages_base_url,
        favicon_path=DEFAULT_PAGE_FAVICON,
):
    assets_directory = Path(assets_directory).resolve(strict=True)
    destination = Path(destination)
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Pages destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    pages_base_url = _normalize_http_url(pages_base_url, "Pages base URL")
    copied_names = []
    sources = sorted(assets_directory.iterdir(), key=lambda item: item.name)
    favicon_candidate = Path(favicon_path)
    if favicon_candidate.is_symlink():
        raise ValueError(f"Pages favicon must be a regular file: {favicon_path}")
    favicon_path = favicon_candidate.resolve(strict=True)
    if not favicon_path.is_file():
        raise ValueError(f"Pages favicon must be a regular file: {favicon_path}")
    total_size = favicon_path.stat().st_size
    for source in sources:
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Pages input must be a regular file: {source}")
        total_size += source.stat().st_size
    if total_size > MAX_PAGES_SITE_BYTES:
        raise ValueError("Pages input exceeds the GitHub Pages site size limit")

    for source in sources:
        shutil.copyfile(source, destination / source.name)
        copied_names.append(source.name)
    shutil.copyfile(favicon_path, destination / "favicon.svg")

    if "result.txt" not in copied_names:
        raise ValueError("Pages input must contain result.txt")

    metadata = _load_page_metadata(assets_directory)
    repository = str(metadata.get("repository") or "").strip()
    generated_at = str(metadata.get("generated_at") or "").strip()
    release_tag = str(metadata.get("release_tag") or "").strip()
    release_guidance = "<p>保存结果请前往当前仓库的 Release 下载对应文件。</p>"
    main_repository_notice = ""
    if REPOSITORY_PATTERN.fullmatch(repository):
        release_path = (
            f"releases/tag/{release_tag}"
            if RELEASE_TAG_PATTERN.fullmatch(release_tag)
            else "releases"
        )
        release_url = f"https://github.com/{repository}/{release_path}"
        release_guidance = f"""<p>保存结果请前往
          <a class="inline-release-link" href="{html.escape(release_url, quote=True)}" target="_blank" rel="noopener noreferrer" aria-label="打开 {html.escape(repository, quote=True)} Release">Release</a>
          下载对应文件。</p>"""
    if repository.lower() == "guovin/iptv-api":
        fork_url = f"https://github.com/{repository}/fork"
        escaped_fork_url = html.escape(fork_url, quote=True)
        main_repository_notice = f"""
    <aside class="test-notice" role="note" aria-labelledby="test-notice-title">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5M12 17.5h.01"/></svg>
      <div>
        <h2 id="test-notice-title">主仓库结果说明 / Upstream results notice</h2>
        <p>主仓库发布的 Pages 链接和 Release 结果仅用于功能测试，不保证内容完整性、可用性或持续更新。实际使用请
          <a class="inline-fork-link" href="{escaped_fork_url}" target="_blank" rel="noopener noreferrer">Fork 项目</a>
          并运行自己的工作流。<br><span lang="en">Pages links and Release results from the upstream repository are for functional testing only.
          <a class="inline-fork-link" href="{escaped_fork_url}" target="_blank" rel="noopener noreferrer">Fork the project</a>
          and run your own workflow for actual use.</span></p>
      </div>
    </aside>"""
    generated_text = html.escape(generated_at or "当前工作流 / Current workflow run")
    generated_markup = (
        f'<time datetime="{html.escape(generated_at, quote=True)}">{generated_text}</time>'
        if generated_at
        else generated_text
    )
    sections_html = _render_page_sections(pages_base_url, copied_names)
    index_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="theme-color" content="#1d4ed8">
  <meta name="description" content="IPTV-API generated playlist, IPv4, IPv6, and EPG result links.">
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <title>IPTV-API 更新结果 / Playlist results</title>
  <style>
    :root {{
      color-scheme: light dark;
      --page: #eff6ff;
      --surface: rgba(255, 255, 255, 0.88);
      --surface-strong: #ffffff;
      --text: #172554;
      --muted: #475569;
      --border: #bfdbfe;
      --primary: #1d4ed8;
      --primary-strong: #1e3a8a;
      --primary-soft: #dbeafe;
      --shadow: 0 18px 50px rgba(30, 64, 175, 0.12);
      --radius-lg: 24px;
      --radius-md: 16px;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      min-height: 100vh;
      margin: 0;
      background:
        radial-gradient(circle at 10% 0%, rgba(59, 130, 246, 0.16), transparent 34rem),
        radial-gradient(circle at 95% 12%, rgba(22, 163, 74, 0.10), transparent 28rem),
        var(--page);
      color: var(--text);
      font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      -webkit-font-smoothing: antialiased;
    }}
    a {{ color: inherit; }}
    .page-shell {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 48px; }}
    .hero {{
      position: relative;
      overflow: hidden;
      padding: clamp(26px, 4.5vw, 48px);
      border: 1px solid rgba(255, 255, 255, 0.28);
      border-radius: var(--radius-lg);
      background: linear-gradient(135deg, #1e3a8a 0%, #1d4ed8 55%, #2563eb 100%);
      box-shadow: var(--shadow);
      color: #ffffff;
    }}
    .hero::after {{
      position: absolute;
      inset: auto -8% -60% auto;
      width: 360px;
      height: 360px;
      border: 72px solid rgba(255, 255, 255, 0.08);
      border-radius: 50%;
      content: "";
    }}
    .brand {{ display: inline-flex; align-items: center; gap: 10px; margin: 0 0 24px; font-weight: 750; letter-spacing: -0.02em; }}
    .brand-mark {{
      width: 34px;
      height: 34px;
      border-radius: 10px;
      background: transparent;
    }}
    .hero-content {{ position: relative; z-index: 1; max-width: 1000px; }}
    h1 {{ max-width: 680px; margin: 0; font-size: clamp(34px, 5vw, 56px); line-height: 1.02; letter-spacing: -0.05em; }}
    .hero-copy {{ max-width: 100%; margin: 16px 0 0; color: #dbeafe; font-size: clamp(16px, 1.7vw, 18px); text-wrap: pretty; }}
    .hero-copy [lang="en"] {{ display: block; margin-top: 4px; color: #bfdbfe; }}
    .hero-meta {{ display: flex; flex-wrap: wrap; gap: 16px 28px; margin-top: 24px; color: #bfdbfe; font-size: 13px; }}
    .hero-meta strong {{ display: block; color: #ffffff; font-size: 18px; }}
    .usage {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 24px 0 48px; }}
    .usage-card {{ padding: 24px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); box-shadow: 0 10px 30px rgba(30, 64, 175, 0.07); backdrop-filter: blur(12px); }}
    .usage-card h2 {{ margin: 0 0 8px; font-size: 18px; letter-spacing: -0.02em; }}
    .usage-card p {{ margin: 0; color: var(--muted); }}
    .inline-release-link {{ color: var(--primary); font-weight: 750; text-decoration-thickness: 1.5px; text-underline-offset: 3px; }}
    .inline-release-link:hover {{ color: var(--primary-strong); }}
    .inline-fork-link {{ color: #92400e; font-weight: 750; text-decoration-thickness: 1.5px; text-underline-offset: 3px; }}
    .inline-fork-link:hover {{ color: #78350f; }}
    .inline-release-link:focus-visible, .inline-fork-link:focus-visible, .card-action:focus-visible {{ outline: 3px solid currentColor; outline-offset: 3px; border-radius: 4px; }}
    .test-notice {{ display: flex; gap: 14px; margin: 24px 0 0; padding: 18px 20px; border: 1px solid #f59e0b; border-radius: var(--radius-md); background: #fffbeb; color: #78350f; }}
    .test-notice svg {{ width: 22px; height: 22px; flex: none; margin-top: 2px; fill: none; stroke: #b45309; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.8; }}
    .test-notice h2 {{ margin: 0 0 4px; font-size: 16px; }}
    .test-notice p {{ margin: 0; font-size: 14px; }}
    .test-notice [lang="en"] {{ color: #92400e; }}
    .result-section {{ margin-top: 48px; }}
    .section-heading {{ max-width: 720px; margin-bottom: 18px; }}
    .eyebrow {{ margin: 0 0 4px; color: var(--primary); font-size: 12px; font-weight: 800; letter-spacing: 0.12em; text-transform: uppercase; }}
    .section-heading h2 {{ margin: 0; font-size: clamp(24px, 3vw, 32px); line-height: 1.2; letter-spacing: -0.035em; }}
    .section-heading > p:last-child {{ margin: 8px 0 0; color: var(--muted); }}
    .result-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .result-card {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 3px 16px;
      min-width: 0;
      padding: 22px;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      background: var(--surface-strong);
      box-shadow: 0 8px 24px rgba(30, 64, 175, 0.06);
      transition: border-color 180ms ease, box-shadow 180ms ease, transform 180ms ease;
    }}
    .result-card:hover {{ border-color: var(--primary); box-shadow: 0 14px 32px rgba(30, 64, 175, 0.13); transform: translateY(-2px); }}
    .file-type {{ grid-column: 2; grid-row: 1 / span 2; align-self: start; padding: 5px 9px; border-radius: 999px; background: var(--primary-soft); color: var(--primary-strong); font-size: 11px; font-weight: 800; letter-spacing: 0.04em; }}
    .result-name {{ grid-column: 1; font-size: 17px; font-weight: 760; letter-spacing: -0.02em; }}
    .result-name-en {{ grid-column: 1; color: var(--muted); font-size: 13px; }}
    .result-card code {{ grid-column: 1 / -1; min-width: 0; margin-top: 14px; padding: 10px 12px; overflow-wrap: anywhere; border-radius: 10px; background: var(--page); color: var(--primary-strong); font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .result-actions {{ display: flex; grid-column: 1 / -1; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    .card-action {{
      display: inline-flex;
      min-height: 42px;
      align-items: center;
      justify-content: center;
      gap: 7px;
      padding: 9px 13px;
      border: 1px solid var(--border);
      border-radius: 10px;
      font: 750 13px/1.3 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      text-decoration: none;
      cursor: pointer;
      transition: background-color 160ms ease, border-color 160ms ease, color 160ms ease;
    }}
    .card-action svg {{ width: 16px; height: 16px; flex: none; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.8; }}
    .copy-action {{ background: transparent; color: var(--primary); }}
    .copy-action:hover {{ border-color: var(--primary); background: var(--primary-soft); }}
    .copy-action.is-copied {{ border-color: #16a34a; background: #dcfce7; color: #166534; }}
    .open-action {{ border-color: var(--primary); background: var(--primary); color: #ffffff; }}
    .open-action:hover {{ border-color: var(--primary-strong); background: var(--primary-strong); }}
    .sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
    footer {{ display: flex; justify-content: space-between; gap: 24px; margin-top: 56px; padding: 24px 0 8px; border-top: 1px solid var(--border); color: var(--muted); font-size: 13px; }}
    footer p {{ margin: 0; }}
    @media (max-width: 980px) {{
      .usage {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 700px) {{
      .page-shell {{ width: min(100% - 24px, 1120px); padding-top: 12px; }}
      .hero {{ padding: 24px 20px 28px; border-radius: 20px; }}
      .brand {{ margin-bottom: 22px; }}
      .result-grid {{ grid-template-columns: 1fr; }}
      .usage {{ margin-bottom: 40px; }}
      .result-section {{ margin-top: 40px; }}
      .result-actions {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      footer {{ flex-direction: column; }}
    }}
    @media (max-width: 420px) {{
      .result-actions {{ grid-template-columns: 1fr; }}
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --page: #081225;
        --surface: rgba(15, 31, 58, 0.86);
        --surface-strong: #0f1f3a;
        --text: #eff6ff;
        --muted: #cbd5e1;
        --border: #27446f;
        --primary: #93c5fd;
        --primary-strong: #dbeafe;
        --primary-soft: #173663;
        --shadow: 0 18px 50px rgba(0, 0, 0, 0.28);
      }}
      .hero {{ background: linear-gradient(135deg, #172554 0%, #1e3a8a 58%, #1d4ed8 100%); }}
      .result-card code {{ background: #081225; color: #bfdbfe; }}
      .test-notice {{ border-color: #92400e; background: #2b1d0b; color: #fef3c7; }}
      .test-notice svg {{ stroke: #fbbf24; }}
      .test-notice [lang="en"] {{ color: #fde68a; }}
      .inline-fork-link, .inline-fork-link:hover {{ color: #fde68a; }}
      .copy-action.is-copied {{ border-color: #4ade80; background: #153d2a; color: #bbf7d0; }}
      .open-action {{ border-color: #2563eb; background: #2563eb; color: #ffffff; }}
      .open-action:hover {{ border-color: #1d4ed8; background: #1d4ed8; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      html {{ scroll-behavior: auto; }}
      .result-card, .card-action {{ transition: none; }}
      .result-card:hover {{ transform: none; }}
    }}
  </style>
</head>
<body>
  <main class="page-shell">
    <header class="hero">
      <div class="hero-content">
        <p class="brand"><img class="brand-mark" src="favicon.svg" alt="" width="34" height="34"> IPTV-API</p>
        <h1>更新结果<br>Playlist results</h1>
        <p class="hero-copy">播放器在线使用请复制下方对应的 Pages 结果地址。<span lang="en">For online player use, copy the applicable Pages result address below.</span></p>
        <div class="hero-meta">
          <span><strong>{len(copied_names)}</strong>可用文件 / Available files</span>
          <span><strong>生成时间 / Generated</strong>{generated_markup}</span>
        </div>
      </div>
    </header>

    {main_repository_notice}

    <section class="usage" aria-label="使用建议 / Usage guidance">
      <article class="usage-card">
        <h2>播放器订阅 / Player subscription</h2>
        <p>播放器在线使用请复制下方对应的 Pages 结果地址。</p>
      </article>
      <article class="usage-card">
        <h2>下载保存 / Download &amp; Save</h2>
        {release_guidance}
      </article>
    </section>

    {sections_html}

    <p class="sr-only" id="copy-status" role="status" aria-live="polite"></p>

    <footer>
      <p>Generated by IPTV-API without committing generated files to Git.</p>
      <p>Pages 用于播放器订阅 · Release 用于下载保存</p>
    </footer>
  </main>
  <script>
    const copyStatus = document.getElementById("copy-status");

    function fallbackCopy(value) {{
      const textarea = document.createElement("textarea");
      textarea.value = value;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      const copied = document.execCommand("copy");
      textarea.remove();
      return copied;
    }}

    async function copyLink(value) {{
      if (navigator.clipboard && window.isSecureContext) {{
        await navigator.clipboard.writeText(value);
        return true;
      }}
      return fallbackCopy(value);
    }}

    document.addEventListener("click", async (event) => {{
      const button = event.target.closest("[data-copy-url]");
      if (!button) return;

      const label = button.querySelector(".action-label");
      const originalLabel = button.dataset.originalLabel || label.textContent;
      button.dataset.originalLabel = originalLabel;
      window.clearTimeout(Number(button.dataset.resetTimer || 0));

      try {{
        if (!await copyLink(button.dataset.copyUrl)) throw new Error("copy failed");
        label.textContent = "已复制 / Copied";
        button.classList.add("is-copied");
        copyStatus.textContent = `已复制 ${{button.dataset.copyUrl}} / Link copied`;
      }} catch (error) {{
        label.textContent = "复制失败 / Copy failed";
        button.classList.remove("is-copied");
        copyStatus.textContent = "复制失败，请手动选择链接 / Copy failed";
      }}

      button.dataset.resetTimer = String(window.setTimeout(() => {{
        label.textContent = originalLabel;
        button.classList.remove("is-copied");
      }}, 1800));
    }});
  </script>
</body>
</html>
"""
    (destination / "index.html").write_text(index_html, encoding="utf-8")
    (destination / "viewer.html").write_text(
        _render_page_viewer(copied_names),
        encoding="utf-8",
    )
    return {
        "pages_base_url": pages_base_url,
        "assets": copied_names,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Prepare public IPTV result files for GitHub Pages and a per-run release."
    )
    parser.add_argument("--final-file", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--generated-at")
    parser.add_argument("--release-tag")
    parser.add_argument("--pages-destination")
    parser.add_argument("--pages-base-url")
    args = parser.parse_args()
    prepare_release_assets(
        final_file=args.final_file,
        destination=args.destination,
        output_dir=args.output_dir,
        repository=os.getenv("GITHUB_REPOSITORY"),
        source_sha=os.getenv("GITHUB_SHA"),
        workflow_run_id=os.getenv("GITHUB_RUN_ID"),
        generated_at=args.generated_at,
        release_tag=args.release_tag,
    )
    if bool(args.pages_destination) != bool(args.pages_base_url):
        parser.error("--pages-destination and --pages-base-url must be used together")
    if args.pages_destination:
        prepare_pages_site(
            assets_directory=args.destination,
            destination=args.pages_destination,
            pages_base_url=args.pages_base_url,
        )


if __name__ == "__main__":
    main()
