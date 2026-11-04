"""Zhihu HTML parsing and Markdown conversion, using only Python's standard library.

Copied from the parent project's extractor on 2026-10-29 and maintained independently.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

class IncompleteContent(ValueError):
    """The page exposes a summary instead of the requested body."""

@dataclass
class Doc:
    """统一的内容模型。"""

    url: str
    kind: str = "unknown"  # article | answer | question
    id: str = ""
    title: str = ""
    author: str = ""
    published: str = ""
    voteup: int = 0
    comment_count: int = 0
    markdown: str = ""
    text: str = ""
    images: list[str] = field(default_factory=list)
    source: str = ""           # 来源类型描述，如「知乎专栏文章」「知乎问答（回答）」
    question_id: str = ""      # 关联问题 ID（回答/问题）
    question_detail: str = ""  # 问题描述（Markdown）
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- HTML → Markdown

_BLOCK_TAGS = {
    "p", "div", "section", "article", "blockquote", "pre", "figure",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "tr",
}
_HEADING_RE = re.compile(r"^h([1-6])$")


class HtmlToMarkdown:
    """轻量 HTML → Markdown 转换器，针对知乎正文结构优化。

    只依赖标准库，避免额外依赖；覆盖段落/标题/列表/引用/代码/
    表格/链接/图片/公式等知乎正文常见结构。
    """

    def __init__(self, keep_images: bool = True) -> None:
        self.keep_images = keep_images
        self.images: list[str] = []

    # -- 公开入口
    def convert(self, html: str) -> str:
        self.images = []
        html = self._preprocess(html)
        root = _parse_html(html)
        if root is None:
            return ""
        md = self._walk(root, depth=0)
        md = self._tidy(md)
        return md

    # -- 预处理：把知乎特有的结构规整成标准标签
    def _preprocess(self, html: str) -> str:
        # 知乎图片懒加载：data-original / data-actualsrc 才是真图
        html = re.sub(
            r'<img([^>]*?)data-original="([^"]+)"([^>]*?)>',
            lambda m: f'<img{m.group(1)} src="{m.group(2)}"{m.group(3)}>', html)
        html = re.sub(
            r'<img([^>]*?)data-actualsrc="([^"]+)"([^>]*?)>',
            lambda m: f'<img{m.group(1)} src="{m.group(2)}"{m.group(3)}>', html)
        # 公式：知乎用 img 承载 TeX，优先取 alt / eeimg
        html = re.sub(r'<img[^>]*class="[^"]*eeimg[^"]*"[^>]*alt="([^"]*)"[^>]*/?>',
                      lambda m: f"<code>{html_mod.escape(m.group(1))}</code>", html)
        # <br> → 换行
        html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
        # 去掉脚本与样式
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html,
                      flags=re.I | re.S)
        return html

    # -- 递归遍历
    def _walk(self, node, depth: int) -> str:
        if isinstance(node, str):
            return _unescape(node)

        tag = node.tag
        children = node.children

        if tag in ("script", "style"):
            return ""

        if tag == "img":
            src = node.attrs.get("src", "")
            alt = node.attrs.get("alt", "")
            if src and not src.startswith("data:"):
                src = _absolutize(src)
                self.images.append(src)
                return f"![{alt}]({src})" if self.keep_images else ""
            return ""

        if tag == "a":
            text = "".join(self._walk(c, depth) for c in children).strip()
            href = node.attrs.get("href", "")
            if not text:
                return ""
            if href and not href.startswith("javascript"):
                return f"[{text}]({_absolutize(href)})"
            return text

        if tag == "code" or tag == "tt":
            inner = "".join(self._walk(c, depth) for c in children)
            return f"`{inner.strip()}`" if inner.strip() else ""

        if tag == "pre":
            # 代码块内沿用 <code> 的文本，但不要带上行内反引号
            inner = self._raw_text(node).strip("\n")
            return f"\n```\n{inner}\n```\n"

        if tag == "hr":
            return "\n\n---\n\n"

        if tag == "br":
            return "\n"

        if tag == "li":
            inner = "".join(self._walk(c, depth) for c in children).strip()
            return f"- {inner}\n"

        if tag == "blockquote":
            inner = "".join(self._walk(c, depth) for c in children).strip()
            quoted = "\n".join("> " + ln for ln in inner.splitlines())
            return f"\n\n{quoted}\n\n"

        if tag == "table":
            return "\n\n" + self._table(node) + "\n\n"

        if tag and _HEADING_RE.match(tag):
            level = int(_HEADING_RE.match(tag).group(1))  # type: ignore[union-attr]
            inner = "".join(self._walk(c, depth) for c in children).strip()
            return f"\n\n{'#' * level} {inner}\n\n"

        # 默认：递归拼接，块级标签后补空行
        inner = "".join(self._walk(c, depth) for c in children)
        if tag in _BLOCK_TAGS:
            if inner.strip():
                return inner.strip() + "\n\n"
            return ""
        return inner

    # -- 取纯文本（不含任何 Markdown 标记），用于代码块等场景
    def _raw_text(self, node) -> str:
        out: list[str] = []

        def walk(n) -> None:
            if isinstance(n, str):
                out.append(_unescape(n))
                return
            if n.tag == "br":
                out.append("\n")
                return
            for c in n.children:
                walk(c)

        walk(node)
        return "".join(out)

    # -- 表格
    def _table(self, node) -> str:
        rows: list[list[str]] = []

        def collect(n) -> None:
            if isinstance(n, str):
                return
            if n.tag == "tr":
                cells = []
                for c in n.children:
                    if not isinstance(c, str) and c.tag in ("td", "th"):
                        cells.append(
                            "".join(self._walk(x, 0) for x in c.children).strip()
                            .replace("\n", " "))
                if cells:
                    rows.append(cells)
                return
            for c in n.children:
                collect(c)

        collect(node)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |",
               "| " + " | ".join(["---"] * width) + " |"]
        for r in rows[1:]:
            out.append("| " + " | ".join(r) + " |")
        return "\n".join(out)

    # -- 收尾清理
    @staticmethod
    def _tidy(md: str) -> str:
        md = re.sub(r"[ \t]+\n", "\n", md)
        md = re.sub(r"\n{3,}", "\n\n", md)
        return md.strip()


def _unescape(s: str) -> str:
    return html_mod.unescape(s)


def _absolutize(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.zhihu.com" + url
    return url


# ---------------------------------------------------------------- 极简 HTML 解析


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent=None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Any] = []
        self.parent = parent


_VOID = {"br", "img", "hr", "meta", "link", "input", "source", "col"}


def _parse_html(html: str) -> Optional[_Node]:
    """用标准库 HTMLParser 构建轻量 DOM，避免引入 bs4 依赖。"""
    from html.parser import HTMLParser

    root = _Node("#root", {})
    stack = [root]

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            node = _Node(tag, {k: (v or "") for k, v in attrs}, stack[-1])
            stack[-1].children.append(node)
            if tag not in _VOID:
                stack.append(node)

        def handle_startendtag(self, tag, attrs):
            node = _Node(tag, {k: (v or "") for k, v in attrs}, stack[-1])
            stack[-1].children.append(node)

        def handle_endtag(self, tag):
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].tag == tag:
                    del stack[i:]
                    break

        def handle_data(self, data):
            stack[-1].children.append(data)

    p = P(convert_charrefs=True)
    try:
        p.feed(html)
        p.close()
    except Exception:
        return root
    return root


def _find_by_id(root: _Node, target_id: str) -> Optional[_Node]:
    if root.attrs.get("id") == target_id:
        return root
    for c in root.children:
        if isinstance(c, str):
            continue
        found = _find_by_id(c, target_id)
        if found is not None:
            return found
    return None


def _find_all_by_class(root: _Node, cls: str) -> list[_Node]:
    out: list[_Node] = []

    def walk(n: _Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                continue
            classes = (c.attrs.get("class") or "").split()
            if cls in classes:
                out.append(c)
            walk(c)

    walk(root)
    return out


# ---------------------------------------------------------------- 提取器


class ZhihuExtractor:
    """从页面 HTML 中抽取正文，不依赖任何第三方解析库。"""

    def __init__(self, keep_images: bool = True) -> None:
        self.conv = HtmlToMarkdown(keep_images=keep_images)

    # ---------- 从 js-initialData 中提取（最稳，结构化）
    def from_initial_data(self, html_text: str, kind: str,
                          ids: tuple[str, ...]) -> Optional[Doc]:
        m = re.search(
            r'<script[^>]+id="js-initialData"[^>]*>(.*?)</script>', html_text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

        try:
            entities = data["initialState"]["entities"]
        except (KeyError, TypeError):
            return None

        if kind == "article":
            art = (entities.get("articles") or {}).get(ids[0])
            if not art:
                return None
            if not str(art.get("content") or "").strip():
                raise IncompleteContent('页面只包含文章摘要或空正文，无法确认完整内容。')
            return self._doc_from_article(art, entities)

        if kind == "answer":
            aid = ids[1] if len(ids) > 1 else ids[0]
            ans = (entities.get("answers") or {}).get(aid)
            if not ans:
                return None
            if not str(ans.get("content") or "").strip():
                raise IncompleteContent('页面未提供回答正文，可能需要重新登录或没有访问权限。')
            return self._doc_from_answer(ans, entities)

        if kind == "question":
            qid = ids[0]
            q = (entities.get("questions") or {}).get(qid)
            if not q:
                return None
            return self._doc_from_question(q, entities)

        return None

    def _doc_from_article(self, art: dict, entities: dict) -> Doc:
        author = (entities.get("users") or {}).get(
            str(art.get("author", {}).get("id", ""))) or {}
        name = art.get("author", {}).get("name") or author.get("name", "")
        content = art.get("content", "")
        body_md = self.conv.convert(content)
        images = list(self.conv.images)

        url = f"https://zhuanlan.zhihu.com/p/{art.get('id', '')}"
        title = art.get("title", "")
        published = _ts_to_str(art.get("created") or art.get("updated"))
        voteup = int(art.get("voteupCount", 0) or 0)
        comment_count = int(art.get("commentCount", 0) or 0)

        markdown = _build_markdown(
            title=title,
            source="知乎专栏文章",
            body=body_md,
            author=name,
            published=published,
            voteup=voteup,
            comment_count=comment_count,
            url=url,
        )
        return Doc(
            url=url,
            kind="article",
            id=str(art.get("id", "")),
            title=title,
            author=name,
            published=published,
            voteup=voteup,
            comment_count=comment_count,
            markdown=markdown,
            text=_md_to_text(markdown),
            images=images,
            source="知乎专栏文章",
        )

    def _doc_from_answer(self, ans: dict, entities: dict) -> Doc:
        author = ans.get("author", {}) or {}
        question = ans.get("question", {}) or {}

        # 补全问题信息（answers 里嵌套的 question 通常只有 id/title，
        # 问题描述在 entities["questions"] 里）
        qid = str(question.get("id", ""))
        full_q = (entities.get("questions") or {}).get(qid) or {}
        title = question.get("title") or full_q.get("title", "")
        q_detail_html = (full_q.get("detail")
                         or question.get("detail", "") or "")

        # 先转换正文，保留正文图片；再转换问题描述，追加描述图片
        content = ans.get("content", "")
        body_md = self.conv.convert(content)
        images = list(self.conv.images)

        q_detail_md = ""
        if q_detail_html:
            q_detail_md = self.conv.convert(q_detail_html)
            images.extend(self.conv.images)

        name = author.get("name", "")
        url = (f"https://www.zhihu.com/question/{qid}"
               f"/answer/{ans.get('id', '')}")
        published = _ts_to_str(ans.get("createdTime") or ans.get("updatedTime"))
        voteup = int(ans.get("voteupCount", 0) or 0)
        comment_count = int(ans.get("commentCount", 0) or 0)

        markdown = _build_markdown(
            title=title,
            source="知乎问答（回答）",
            body=body_md,
            author=name,
            published=published,
            voteup=voteup,
            comment_count=comment_count,
            url=url,
            question_detail=q_detail_md,
        )
        doc = Doc(
            url=url,
            kind="answer",
            id=str(ans.get("id", "")),
            title=title,
            author=name,
            published=published,
            voteup=voteup,
            comment_count=comment_count,
            markdown=markdown,
            text=_md_to_text(markdown),
            images=images,
            source="知乎问答（回答）",
            question_id=qid,
            question_detail=q_detail_md,
        )
        doc.extra["question_id"] = qid
        return doc

    def _doc_from_question(self, q: dict, entities: dict) -> Doc:
        qid = str(q.get("id", ""))
        title = q.get("title", "")
        detail_html = q.get("detail", "") or ""
        detail_md = self.conv.convert(detail_html) if detail_html else ""
        images = list(self.conv.images)

        url = f"https://www.zhihu.com/question/{qid}"
        published = _ts_to_str(q.get("created") or q.get("updatedTime"))
        voteup = int(q.get("voteupCount", 0) or 0)
        comment_count = int(q.get("commentCount", 0) or 0)

        markdown = _build_markdown(
            title=title,
            source="知乎问答（问题）",
            body=detail_md,
            published=published,
            voteup=voteup,
            comment_count=comment_count,
            url=url,
        )
        return Doc(
            url=url,
            kind="question",
            id=qid,
            title=title,
            published=published,
            voteup=voteup,
            comment_count=comment_count,
            markdown=markdown,
            text=_md_to_text(markdown),
            images=images,
            source="知乎问答（问题）",
            question_id=qid,
            question_detail=detail_md,
        )

    # ---------- 从渲染后的 DOM 文本提取（兜底）
    def from_dom(self, html_text: str, kind: str, url: str) -> Optional[Doc]:
        root = _parse_html(html_text)
        if root is None:
            return None

        # 标题
        title = ""
        for tid in ("zh-question-title",):
            n = _find_by_id(root, tid)
            if n is not None:
                title = _node_text(n).strip()
                break
        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S)
            if m:
                title = _unescape(m.group(1)).replace(" - 知乎", "").strip()
        if not title:
            h1 = _find_all_by_class(root, "Post-Title")
            if h1:
                title = _node_text(h1[0]).strip()

        # 正文
        body: Optional[_Node] = None
        for cid in ("zh-question-answer-wrap", "manuscript"):
            body = _find_by_id(root, cid)
            if body is not None:
                break
        if body is None:
            for cls in ("RichText", "RichContent-inner", "Post-RichText"):
                found = _find_all_by_class(root, cls)
                if found:
                    body = found[0]
                    break
        if body is None:
            return None

        body_md = self.conv.convert(_serialize(body))
        if len(body_md) < 80:
            return None

        source_map = {
            "article": "知乎专栏文章",
            "answer": "知乎问答（回答）",
            "question": "知乎问答（问题）",
        }
        source = source_map.get(kind, "知乎")

        markdown = _build_markdown(
            title=title,
            source=source,
            body=body_md,
            url=url,
        )

        return Doc(
            url=url,
            kind=kind,
            id=url.rstrip("/").rsplit("/", 1)[-1],
            title=title,
            markdown=markdown,
            text=_md_to_text(markdown),
            images=list(self.conv.images),
            source=source,
        )


def _node_text(node: _Node) -> str:
    parts: list[str] = []

    def walk(n: _Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                parts.append(c)
            else:
                walk(c)

    walk(node)
    return re.sub(r"\s+", " ", "".join(parts))


def _serialize(node: _Node) -> str:
    """把 DOM 子树还原成 HTML 字符串（供 Markdown 转换器复用）。"""
    if isinstance(node, str):  # pragma: no cover
        return node
    attrs = "".join(f' {k}="{html_mod.escape(str(v))}"'
                    for k, v in node.attrs.items())
    inner = "".join(_serialize(c) if not isinstance(c, str)
                    else html_mod.escape(c) for c in node.children)
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


def _build_markdown(
    *,
    title: str,
    source: str,
    body: str,
    author: str = "",
    published: str = "",
    voteup: int = 0,
    comment_count: int = 0,
    url: str = "",
    question_detail: str = "",
) -> str:
    """构建带有来源元信息头的 Markdown 文档。

    头部结构：
        # 标题
        > **来源**：知乎专栏文章 / 知乎问答（回答）
        > **作者**：xxx
        > **发布时间**：YYYY-MM-DD
        > **互动数据**：赞同 N · 评论 M
        > **原文链接**：https://...

        ## 问题描述（仅回答）
        ...

        ---
        正文
    """
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")

    meta_lines: list[str] = [f"**来源**：{source}"]
    if author:
        meta_lines.append(f"**作者**：{author}")
    if published:
        meta_lines.append(f"**发布时间**：{published}")

    stats: list[str] = []
    if voteup:
        stats.append(f"赞同 {voteup}")
    if comment_count:
        stats.append(f"评论 {comment_count}")
    if stats:
        meta_lines.append(f"**互动数据**：{' · '.join(stats)}")

    if url:
        meta_lines.append(f"**原文链接**：{url}")

    parts.append("")
    parts.extend(f"> {line}" for line in meta_lines)

    if question_detail.strip():
        parts.append("")
        parts.append("## 问题描述")
        parts.append("")
        parts.append(question_detail.strip())

    if body.strip():
        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append(body.strip())

    return "\n".join(parts).strip() + "\n"


def _md_to_text(md: str) -> str:
    """Markdown → 纯文本，方便喂给 LLM 或做全文检索。"""
    t = re.sub(r"^```[^\n]*\n(.*?)^```[ \t]*$", r"\1", md, flags=re.M | re.S)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\*{1,3}([^*\n]+?)\*{1,3}", r"\1", t)   # 去掉 **bold** / *italic*
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    # 表格：把 | 分隔的行转成空格分隔，去掉分隔线
    t = re.sub(r"^\s*\|[\s:|-]+\|\s*$", "", t, flags=re.M)
    t = re.sub(r"^\s*\|(.*)\|\s*$",
               lambda m: m.group(1).replace("|", " "), t, flags=re.M)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _ts_to_str(ts: Any) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except (ValueError, TypeError, OSError):
        return str(ts)


