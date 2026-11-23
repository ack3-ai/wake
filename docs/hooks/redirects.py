"""Redirect URLs of removed or renamed documentation pages to their successors.

Each old page gets a small HTML stub (meta refresh, JavaScript redirect and a
canonical link) at its former location, so external links and indexed URLs keep
working. Keys and values are source paths relative to `docs/`.
"""

from __future__ import annotations

import html
import logging
import posixpath
from pathlib import Path

log = logging.getLogger("mkdocs.hooks.redirects")

REDIRECTS = {
    # the detectors page moved under static analysis in 4.0.0
    "detectors.md": "static-analysis/using-detectors.md",
    # wake.ir.yul.case_statement was renamed to wake.ir.yul.case_
    "api-reference/ir/yul/case-statement.md": "api-reference/ir/yul/case.md",
}

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Redirecting...</title>
<link rel="canonical" href="{canonical}">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="0; url={target}">
<script>location.replace("{target}" + location.hash)</script>
</head>
<body>
Redirecting to <a href="{target}">{target}</a>...
</body>
</html>
"""


def _dest_uri(src: str, use_directory_urls: bool) -> str:
    stem = src[: -len(".md")]
    if not use_directory_urls or posixpath.basename(stem) == "index":
        return stem + ".html"
    return stem + "/index.html"


def _url(dest_uri: str) -> str:
    return (
        dest_uri[: -len("index.html")] if dest_uri.endswith("index.html") else dest_uri
    )


def on_files(files, config, **kwargs):
    for old, new in REDIRECTS.items():
        if files.get_file_from_path(old) is not None:
            log.warning("Redirect source %s still exists as a page", old)
        if files.get_file_from_path(new) is None:
            log.warning("Redirect target %s of %s does not exist", new, old)
    return files


def on_post_build(config, **kwargs):
    directory_urls = config.use_directory_urls
    site_url = config.site_url or ""
    for old, new in REDIRECTS.items():
        old_dest = _dest_uri(old, directory_urls)
        new_url = _url(_dest_uri(new, directory_urls))
        target = posixpath.relpath(new_url or ".", posixpath.dirname(old_dest) or ".")
        if new_url.endswith("/") or not new_url:
            target += "/"
        canonical = site_url.rstrip("/") + "/" + new_url if site_url else target
        path = Path(config.site_dir) / old_dest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _TEMPLATE.format(
                target=html.escape(target), canonical=html.escape(canonical)
            ),
            encoding="utf-8",
        )
