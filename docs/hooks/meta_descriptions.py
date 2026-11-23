"""Give every documentation page its own meta description.

A page's front matter `description:` always wins. Pages without one get a
description derived from their own content:

- API reference pages (a bare `::: wake.module` directive) use the first
  plain sentence of the module docstring, or of the first documented class.
- Other pages use their first prose paragraph.

The text is trimmed to 155 characters. If a derived description collides with
one already used, the page title is prepended so that no two pages share one.
The hook runs before the social plugin, so Open Graph tags and social cards
pick up the same description.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional

from mkdocs.plugins import event_priority

log = logging.getLogger("mkdocs.hooks.meta_descriptions")

MAX_LENGTH = 155

# sentence boundary, but not after "e.g." or "i.e."
_SENTENCE_END = re.compile(r"(?<![ (]e\.g\.)(?<![ (]i\.e\.)(?<=[.!?])\s+")

_seen: Dict[str, str] = {}
_search_paths: list = []


def on_config(config, **kwargs):
    _search_paths[:] = [str(Path(config.config_file_path).parent)]
    return config


def on_pre_build(config, **kwargs):
    _seen.clear()


def _strip_markdown(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)  # images
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)  # [text][ref]
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [text](url)
    text = re.sub(r"`(?::::\w+\s+)?([^`]*)`", r"\1", text)  # `code`, `:::lang code`
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)  # bold
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)  # italics
    text = text.replace("\u2014", ", ").replace("\u2013", "-")  # no em/en dashes
    text = text.replace('"', "'")  # Material does not escape the meta attribute
    text = re.sub(r"\s+,", ",", text)
    return " ".join(text.split())


def _trim(text: str) -> str:
    if len(text) <= MAX_LENGTH:
        return text
    out = ""
    for sentence in _SENTENCE_END.split(text):
        candidate = f"{out} {sentence}".strip()
        if len(candidate) > MAX_LENGTH:
            break
        out = candidate
    if out:
        return out
    cut = text[: MAX_LENGTH - 1].rsplit(" ", 1)[0].rstrip(" ,;:(-")
    return cut + "\u2026"


def _first_paragraph(markdown: str) -> Optional[str]:
    """First prose paragraph: skips headings, code, admonitions, lists, tables, HTML."""
    in_fence = False
    in_html = False
    paragraph: list = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if in_html:
            in_html = bool(stripped)
            continue
        if stripped.startswith("<") and not paragraph:
            in_html = True
            continue
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            if paragraph:
                break
            continue
        if in_fence:
            continue
        if not stripped:
            if paragraph:
                break
            continue
        if paragraph:
            paragraph.append(stripped)
            continue
        if (
            line.startswith((" ", "\t"))
            or stripped.startswith(
                ("#", "!!!", "???", ":::", "|", "<", ">", "-", "*", "+", "{")
            )
            or re.match(r"\d+\.\s", stripped)
            or re.match(r"Name:\s", stripped)
        ):
            continue
        paragraph.append(stripped)
    text = _strip_markdown(" ".join(paragraph))
    if text.endswith(":"):  # a paragraph introducing a list or code block
        text = text[:-1] + "."
    return text or None


def _first_sentence(docstring: str) -> Optional[str]:
    first = docstring.strip().split("\n\n")[0]
    if first.lstrip().startswith(("!!!", "???", "```")):
        return None
    text = _strip_markdown(first)
    sentence = _SENTENCE_END.split(text)[0]
    if not sentence or sentence.endswith(":") or len(sentence) < 20:
        return None
    return sentence


def _api_description(module_path: str) -> Optional[str]:
    try:
        import griffe

        module = griffe.GriffeLoader(search_paths=_search_paths).load(module_path)
    except Exception as e:  # never fail the docs build over a description
        log.debug("could not load %s: %s", module_path, e)
        return None

    classes = [
        name
        for name, member in module.members.items()
        if not name.startswith("_") and not member.is_alias and member.is_class
    ]
    # prefer the class the module is named after, e.g. ImportDirective in import_directive
    segment = module_path.rsplit(".", 1)[-1].replace("_", "")
    main = [name for name in classes if name.lower().endswith(segment)][:1]
    classes = main + [name for name in classes if name not in main]

    sentence = _first_sentence(module.docstring.value) if module.docstring else None
    for name in main or classes:
        if sentence:
            break
        docstring = module.members[name].docstring
        sentence = _first_sentence(docstring.value) if docstring else None

    if sentence:
        return _trim(f"{module_path}: {sentence}")
    if classes:
        listed = ", ".join(classes[:3]) + (" and more" if len(classes) > 3 else "")
        return _trim(f"Wake API reference for {module_path}, documenting {listed}.")
    return _trim(f"Wake API reference for the {module_path} module.")


@event_priority(100)  # before the social plugin reads page.meta
def on_page_markdown(markdown, page, config, files, **kwargs):
    explicit = page.meta.get("description")
    if explicit:
        description = " ".join(str(explicit).split()).replace('"', "'")
        if description in _seen:
            log.warning(
                "Description of %s duplicates %s", page.file.src_uri, _seen[description]
            )
        if len(description) > MAX_LENGTH:
            log.warning(
                "Description of %s is %d characters long (max %d)",
                page.file.src_uri,
                len(description),
                MAX_LENGTH,
            )
    else:
        # a page made only of `::: wake.module` directives (plus indented options)
        lines = markdown.strip().splitlines()
        directive = re.match(r":::\s*(wake[\w.]*)\s*$", lines[0]) if lines else None
        if directive and all(
            not line.strip() or line.startswith((" ", "\t", ":::")) for line in lines
        ):
            description = _api_description(directive.group(1))
        else:
            description = None
        if description is None:
            paragraph = _first_paragraph(markdown)
            description = _trim(paragraph) if paragraph else None
        if description is None:
            description = _trim(f"{page.title} in the Wake documentation.")
        if description in _seen:
            description = _trim(f"{page.title}: {description}")

    page.meta["description"] = description
    _seen.setdefault(description, page.file.src_uri)
    return markdown
