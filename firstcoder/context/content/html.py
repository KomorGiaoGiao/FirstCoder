"""html 输出的确定性压缩器。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from firstcoder.context.content.router import RouteCompactResult, RouteContentType, RouteContext
from firstcoder.context.models import MessagePart


_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas"}
_HEADING_TAGS = {"h1", "h2", "h3"}


@dataclass(slots=True)
class HtmlRouteCompressor:
    max_text_blocks: int = 40
    max_links: int = 20
    max_block_chars: int = 240

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult | None:
        parser = _VisibleHtmlParser(max_block_chars=self.max_block_chars)
        try:
            parser.feed(part.content)
            parser.close()
        except Exception:
            return None

        if not parser.looks_like_html:
            return None

        text_blocks = _dedupe_preserve_order(parser.text_blocks)
        visible_blocks = _select_visible_blocks(text_blocks, max_blocks=self.max_text_blocks)
        links = parser.links[: self.max_links]

        lines = ["[HTML compacted]"]
        if parser.title:
            lines.append(f"title: {parser.title}")
        if parser.headings:
            lines.append("headings:")
            for heading in parser.headings[:20]:
                lines.append(f"- {heading}")
        if visible_blocks:
            lines.append("text:")
            for block in visible_blocks:
                lines.append(f"- {block}")
        if links:
            lines.append("links:")
            for text, href in links:
                label = text or href
                lines.append(f"- {label} -> {href}")

        omitted_blocks = max(0, len(text_blocks) - len(visible_blocks))
        hidden_links = max(0, len(parser.links) - len(links))
        if omitted_blocks:
            lines.append(f"[... omitted {omitted_blocks} text blocks]")
        if hidden_links:
            lines.append(f"[... omitted {hidden_links} links]")

        return RouteCompactResult(
            content="\n".join(lines),
            content_type=RouteContentType.HTML,
            compacted_by="l2_html",
            metadata={
                "html_title": parser.title,
                "html_heading_count": len(parser.headings),
                "html_text_blocks": len(text_blocks),
                "html_kept_text_blocks": len(visible_blocks),
                "html_omitted_text_blocks": omitted_blocks,
                "html_links": len(parser.links),
                "html_hidden_links": hidden_links,
            },
        )


@dataclass
class _VisibleHtmlParser(HTMLParser):
    max_block_chars: int
    looks_like_html: bool = False
    title: str | None = None
    headings: list[str] = field(default_factory=list)
    text_blocks: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    _tag_stack: list[str] = field(default_factory=list)
    _current_link_href: str | None = None
    _current_link_text: list[str] = field(default_factory=list)
    _current_title: list[str] = field(default_factory=list)
    _current_heading: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        HTMLParser.__init__(self)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self.looks_like_html = True
        self._tag_stack.append(tag)
        if tag == "a":
            href = next((value for name, value in attrs if name.lower() == "href" and value), None)
            self._current_link_href = href
            self._current_link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._current_title:
            self.title = _clean_text(" ".join(self._current_title))
            self._current_title = []
        if tag in _HEADING_TAGS and self._current_heading:
            heading = _clean_text(" ".join(self._current_heading))
            if heading:
                self.headings.append(heading)
            self._current_heading = []
        if tag == "a" and self._current_link_href:
            label = _clean_text(" ".join(self._current_link_text))
            self.links.append((label, self._current_link_href))
            self._current_link_href = None
            self._current_link_text = []
        if tag in self._tag_stack:
            index = len(self._tag_stack) - 1 - self._tag_stack[::-1].index(tag)
            del self._tag_stack[index:]

    def handle_data(self, data: str) -> None:
        if any(tag in _SKIP_TAGS for tag in self._tag_stack):
            return
        text = _clean_text(data)
        if not text:
            return
        current_tag = self._tag_stack[-1] if self._tag_stack else ""
        if current_tag == "title":
            self._current_title.append(text)
            return
        if current_tag in _HEADING_TAGS:
            self._current_heading.append(text)
        if self._current_link_href is not None:
            self._current_link_text.append(text)
        if len(text) > self.max_block_chars:
            text = text[: self.max_block_chars].rstrip() + "...[truncated]"
        self.text_blocks.append(text)


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _select_visible_blocks(blocks: list[str], *, max_blocks: int) -> list[str]:
    if len(blocks) <= max_blocks:
        return list(blocks)

    selected: list[str] = list(blocks[:max_blocks])
    for block in sorted(blocks[max_blocks:], key=_block_score, reverse=True):
        if _block_score(block) <= 0:
            break
        if block in selected:
            continue
        if len(selected) >= max_blocks:
            selected.pop()
        selected.append(block)
        if len(selected) >= max_blocks:
            break
    return selected


def _block_score(block: str) -> int:
    lowered = block.lower()
    score = 0
    for keyword in ("error", "failed", "fail", "traceback", "exception", "todo", "fixme", "warning", "sentinel"):
        if keyword in lowered:
            score += 10
    if re.search(r"\b[A-Z][A-Z0-9_]{6,}\b", block):
        score += 8
    return score
