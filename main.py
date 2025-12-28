import re
import html as htmllib
import requests
from dataclasses import dataclass, field
from bs4 import BeautifulSoup, Tag, NavigableString


class RentryArchiver:
    WS_RE = re.compile(r"[ \t\r\f\v]+")
    ALIGN_STYLE_RE = re.compile(r"text-align\s*:\s*(left|center|right)\b", re.I)

    @dataclass
    class _Ctx:
        in_pre: bool = False
        list_depth: int = 0
        in_heading: bool = False
        heading_id_level: dict[str, int] = field(default_factory=dict)

    def archive(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")

        article = (
            soup.select_one(".render-metadata .entry-text article")
            or soup.find("article")
            or soup.find("main")
            or soup.body
        )
        if not article:
            raise RuntimeError("Could not locate main content area")

        heading_id_level: dict[str, int] = {}
        for h in article.find_all(re.compile(r"^h[1-6]$", re.I)):
            hid = h.get("id")
            if hid:
                try:
                    heading_id_level[hid] = int(h.name[1])
                except Exception:
                    pass

        raw = self._render_children_blocks(article, self._Ctx(heading_id_level=heading_id_level))
        return re.sub(r"\n{3,}", "\n\n", raw).strip() + "\n"

    def archive_url(self, url: str, *, timeout: int = 30) -> str:
        _user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        resp = requests.get(url, headers={"User-Agent": _user_agent}, timeout=timeout)
        resp.raise_for_status()
        return self.archive(resp.text)

    def _collapse_ws(self, s: str) -> str:
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        return "\n".join(self.WS_RE.sub(" ", line) for line in s.split("\n"))

    def _strip_blank_lines(self, lines: list[str]) -> list[str]:
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return lines

    def _indent_lines(self, text: str, n: int) -> str:
        pad = " " * n
        return "\n".join(pad + line if line.strip() else line for line in text.splitlines())

    def _is_all_whitespace_node(self, node) -> bool:
        return isinstance(node, NavigableString) and not str(node).strip()

    def _unwrap_header_text(self, h: Tag) -> list:
        out = []
        for c in h.children:
            if isinstance(c, Tag) and c.name == "a" and "headerlink" in (c.get("class") or []):
                continue
            out.append(c)
        return out

    def _detect_cell_align(self, cell: Tag) -> str | None:
        if not isinstance(cell, Tag):
            return None

        style = cell.get("style") or ""
        m = self.ALIGN_STYLE_RE.search(style)
        if m:
            return m.group(1).lower()

        align_attr = (cell.get("align") or "").strip().lower()
        if align_attr in ("left", "center", "right"):
            return align_attr

        cls = cell.get("class") or []
        if "md-center" in cls:
            return "center"
        if "md-right" in cls:
            return "right"

        return None

    def _render_children_inlines(self, node: Tag, ctx: _Ctx) -> str:
        return "".join(self._render_inlines(c, ctx) for c in node.children)

    def _render_inlines(self, node, ctx: _Ctx) -> str:
        if isinstance(node, NavigableString):
            s = str(node)
            if ctx.in_pre:
                return s
            return self._collapse_ws(s).replace("\n", " ")

        if not isinstance(node, Tag):
            return ""

        name = node.name.lower()
        classes = node.get("class") or []

        if name == "br":
            return "\n"

        if name == "a" and "headerlink" in classes:
            return ""

        if name in ("b", "strong"):
            return f"**{self._render_children_inlines(node, ctx).strip()}**"
        if name in ("i", "em"):
            return f"*{self._render_children_inlines(node, ctx).strip()}*"
        if name in ("s", "del"):
            return f"~~{self._render_children_inlines(node, ctx).strip()}~~"
        if name == "mark":
            return f"=={self._render_children_inlines(node, ctx).strip()}=="
        if name == "code":
            return f"`{self._render_children_inlines(node, ctx).strip()}`"

        if name == "span" and "md-align" in classes:
            inner = self._render_children_inlines(node, ctx).strip()
            if ctx.in_heading:
                return inner
            if "md-center" in classes:
                return f"-> {inner} <-"
            if "md-right" in classes:
                return f"-> {inner} ->"
            return inner

        if name == "span" and "color-change" in classes:
            style = (node.get("style") or "")
            m = re.search(r"color\s*:\s*([^;]+)", style, flags=re.I)
            color = m.group(1).strip() if m else ""
            inner = self._render_children_inlines(node, ctx).strip()
            return f"%{color}%{inner}%%" if color else inner

        if name == "span" and "spoiler" in classes:
            inner = self._render_children_inlines(node, ctx).strip()
            return f"||{inner}||"

        if name == "a":
            href = node.get("href") or ""
            text = self._render_children_inlines(node, ctx).strip()
            if text and href and text == href:
                return href
            if not text:
                return href
            return f"[{text}]({href})"

        if name == "img":
            alt = node.get("alt") or ""
            src = node.get("src") or ""
            return f"![{alt}]({src})"

        return self._render_children_inlines(node, ctx)

    def _render_children_blocks(self, node: Tag, ctx: _Ctx) -> str:
        parts = []
        for c in node.children:
            if self._is_all_whitespace_node(c):
                continue
            parts.append(self._render_block(c, ctx))
        return "".join(parts)

    def _render_block(self, node, ctx: _Ctx) -> str:
        if isinstance(node, NavigableString):
            if not str(node).strip():
                return ""
            return self._render_inlines(node, ctx).strip() + "\n\n"

        if not isinstance(node, Tag):
            return ""

        name = node.name.lower()
        classes = node.get("class") or []

        if name == "div" and "toc" in classes:
            token = "[TOC]"
            first_a = node.find("a", href=True)
            if first_a:
                href = first_a.get("href", "")
                if href.startswith("#"):
                    hid = href[1:]
                    lvl = ctx.heading_id_level.get(hid)
                    if isinstance(lvl, int) and 1 <= lvl <= 6:
                        token = "[TOC]" if lvl == 1 else f"[TOC{lvl}]"
            return f"{token}\n\n"

        if name == "div" and "codeblock" in classes:
            clippy = node.select_one(".clippy")
            if clippy and clippy.has_attr("value"):
                raw = htmllib.unescape(clippy["value"])
                raw = raw.replace("\r\n", "\n").replace("\r", "\n")
                return f"```\n{raw.rstrip()}\n```\n\n"
            txt = node.get_text("\n")
            return f"```\n{txt.rstrip()}\n```\n\n"

        if name == "div" and "admonition" in classes:
            kind = None
            for k in ("note", "info", "warning", "danger", "greentext"):
                if k in classes:
                    kind = k
                    break
            kind = kind or "note"

            title_tag = node.select_one(":scope > p.admonition-title")
            title = self._render_children_inlines(title_tag, ctx).strip() if title_tag else ""

            body_parts = []
            for child in node.children:
                if isinstance(child, Tag) and child.name == "p" and "admonition-title" in (child.get("class") or []):
                    continue
                if self._is_all_whitespace_node(child):
                    continue
                body_parts.append(self._render_block(child, ctx).rstrip("\n"))

            body = "\n".join([p for p in body_parts if p.strip()]).strip()
            header = f"!!! {kind}" + (f" {title}" if title else "")
            if not body:
                return header + "\n\n"
            return header + "\n" + self._indent_lines(body, 4) + "\n\n"

        if name == "hr":
            return "---\n\n"

        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(name[1])

            aligned = None
            if "md-center" in classes:
                aligned = "center"
            elif "md-right" in classes:
                aligned = "right"
            else:
                for child in node.children:
                    if isinstance(child, Tag) and child.name.lower() == "span":
                        ccls = child.get("class") or []
                        if "md-align" in ccls:
                            if "md-center" in ccls:
                                aligned = "center"
                            elif "md-right" in ccls:
                                aligned = "right"
                            break

            hctx = self._Ctx(
                in_pre=ctx.in_pre,
                list_depth=ctx.list_depth,
                in_heading=True,
                heading_id_level=ctx.heading_id_level,
            )

            text = "".join(self._render_inlines(c, hctx) for c in self._unwrap_header_text(node)).strip()

            if aligned == "center":
                text = f"-> {text} <-"
            elif aligned == "right":
                text = f"-> {text} ->"

            return f"{'#' * level} {text}\n\n"

        if name == "p":
            span_aligns = node.find_all("span", class_=lambda c: c and "md-align" in c)
            only_align = True
            for c in node.children:
                if self._is_all_whitespace_node(c):
                    continue
                if isinstance(c, Tag) and c.name == "span" and "md-align" in (c.get("class") or []):
                    continue
                only_align = False
                break

            if only_align and span_aligns:
                lines = [self._render_inlines(s, ctx).strip() for s in span_aligns]
                return "\n".join(lines).rstrip() + "\n\n"

            text = self._render_children_inlines(node, ctx).strip()
            return (text + "\n\n") if text else ""

        if name == "blockquote":
            inner = self._render_children_blocks(node, ctx).strip("\n")
            inner_lines = self._strip_blank_lines(inner.splitlines())
            out = "\n".join([("> " + ln) if ln.strip() else ">" for ln in inner_lines])
            return out + "\n\n"

        if name in ("ul", "ol"):
            return self._render_list(node, ctx)

        if name == "div" and "ntable-wrapper" in classes:
            tbl = node.find("table")
            return self._render_block(tbl, ctx) if tbl else ""

        if name == "table":
            return self._render_table(node, ctx)

        if name == "pre":
            txt = node.get_text("\n")
            return f"```\n{txt.rstrip()}\n```\n\n"

        if name == "span" and "clear-floats" in classes:
            return "!;\n\n"

        return self._render_children_blocks(node, ctx)

    def _render_list(self, list_tag: Tag, ctx: _Ctx) -> str:
        ordered = (list_tag.name.lower() == "ol")
        depth = ctx.list_depth
        base_indent = 4 * depth

        lines: list[str] = []
        li_tags = list_tag.find_all("li", recursive=False)

        for idx, li in enumerate(li_tags):
            marker = f"{idx+1}." if ordered else "-"
            item_lines = self._render_list_item(
                li,
                self._Ctx(
                    in_pre=False,
                    list_depth=depth,
                    in_heading=False,
                    heading_id_level=ctx.heading_id_level,
                ),
            )
            if not item_lines:
                continue

            lines.append((" " * base_indent) + f"{marker} {item_lines[0]}")
            cont_indent = base_indent + 2
            for extra in item_lines[1:]:
                if extra == "":
                    lines.append("")
                else:
                    lines.append((" " * cont_indent) + extra)

        return "\n".join(lines).rstrip() + "\n\n"

    def _render_list_item(self, li: Tag, ctx: _Ctx) -> list[str]:
        classes = li.get("class") or []
        is_task = "task-list" in classes
        checkbox = li.find("input", attrs={"type": "checkbox"})
        checked = bool(checkbox and checkbox.has_attr("checked"))

        nested_lists = []
        content_nodes = []
        for c in li.children:
            if self._is_all_whitespace_node(c):
                continue
            if isinstance(c, Tag) and c.name.lower() in ("ul", "ol"):
                nested_lists.append(c)
            else:
                content_nodes.append(c)

        paragraphs: list[str] = []
        buffer_inline: list[str] = []

        def flush_inline_as_para():
            nonlocal buffer_inline
            s = "".join(buffer_inline).strip()
            if s:
                paragraphs.append(s)
            buffer_inline = []

        for c in content_nodes:
            if isinstance(c, Tag) and c.name.lower() == "p":
                flush_inline_as_para()
                ptxt = self._render_children_inlines(c, ctx).strip()
                if ptxt:
                    paragraphs.append(ptxt)
            else:
                buffer_inline.append(self._render_inlines(c, ctx))
        flush_inline_as_para()

        if is_task and checkbox:
            prefix = "[x]" if checked else "[ ]"
            if paragraphs:
                paragraphs[0] = f"{prefix} {paragraphs[0]}".strip()
            else:
                paragraphs = [prefix]

        if not paragraphs:
            paragraphs = [""]

        lines: list[str] = []
        for pi, p in enumerate(paragraphs):
            if pi > 0:
                lines.append("")
            lines.extend(p.splitlines())

        for nl in nested_lists:
            nested = self._render_list(
                nl,
                self._Ctx(
                    in_pre=False,
                    list_depth=ctx.list_depth + 1,
                    in_heading=False,
                    heading_id_level=ctx.heading_id_level,
                ),
            ).rstrip("\n")
            if nested:
                if lines and lines[-1] != "":
                    lines.append("")
                lines.extend(nested.splitlines())

        return lines

    def _render_table(self, tbl: Tag, ctx: _Ctx) -> str:
        rows = tbl.find_all("tr")
        if not rows:
            return ""

        header_cells = rows[0].find_all(["th", "td"], recursive=False)
        headers = [self._render_children_inlines(c, ctx).strip() for c in header_cells]

        aligns = []
        for c in header_cells:
            a = self._detect_cell_align(c)
            if a == "center":
                aligns.append(":---:")
            elif a == "right":
                aligns.append("---:")
            else:
                aligns.append("---")

        out = []
        out.append("| " + " | ".join(headers) + " |")
        out.append("| " + " | ".join(aligns) + " |")

        for r in rows[1:]:
            cells = r.find_all(["td", "th"], recursive=False)
            vals = [self._render_children_inlines(c, ctx).strip().replace("\n", "\\n") for c in cells]
            out.append("| " + " | ".join(vals) + " |")

        return "\n".join(out) + "\n\n"


if __name__ == "__main__":
    archiver = RentryArchiver()
    md = archiver.archive_url("https://rentry.co/megathread")
    with open("rentry.md", "w", encoding="utf-8") as f:
        f.write(md)