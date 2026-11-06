# -*- coding: utf-8 -*-
"""Shared helpers for filetools tools. Tools may `import common`.
Everything here is pure computation over the uploaded file list —
no filesystem access (the server never sees the real files)."""
import re


def natural_key(s):
    """Sort key where digit runs compare numerically: a2 < a10."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", str(s))]


def basename(path):
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def dirname(path):
    p = str(path).replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def ext_of(name):
    b = basename(name)
    return b.rsplit(".", 1)[-1].lower() if "." in b[1:] else ""


def size_bucket(size, edges_mb=(100, 500)):
    """Return a label like '小于100M' / '100M-500M' / '大于500M'."""
    edges = sorted(edges_mb)
    prev = 0
    for e in edges:
        if size < e * 1024 * 1024:
            return ("小于%dM" % e) if prev == 0 else ("%dM-%dM" % (prev, e))
        prev = e
    return "大于%dM" % prev


def safe_seg(s):
    """Make one path segment safe: no separators, no leading dots."""
    s = re.sub(r'[\\/:*?"<>|]', "_", str(s)).strip()
    return s.lstrip(".") or "_"
