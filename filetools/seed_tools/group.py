# -*- coding: utf-8 -*-
"""示例工具：把全部文件按路径顺序分组，新建文件夹存放，
每个文件夹放 per_folder 个（默认 2 个）。"""

META = {
    "id": "group",
    "name": "分组到新文件夹",
    "desc": "全部文件按顺序分组，每组一个新文件夹",
    "needs": [],
    "params": [
        {"key": "per_folder", "label": "每个文件夹文件数", "type": "number",
         "default": 2, "min": 1, "max": 10000},
        {"key": "prefix", "label": "文件夹名前缀", "type": "text",
         "default": "分组"},
        {"key": "start", "label": "起始编号", "type": "number",
         "default": 1, "min": 0},
    ],
}


def plan(files, params):
    import common
    per = int(params.get("per_folder") or 2)
    if per < 1:
        per = 1
    prefix = common.safe_seg(params.get("prefix") or "分组")
    start = int(params.get("start") or 1)
    paths = sorted((f["path"] for f in files), key=common.natural_key)
    ops = []
    for i, p in enumerate(paths):
        folder = "%s%03d" % (prefix, start + i // per)
        ops.append({"from": p, "to": folder + "/" + common.basename(p)})
    return ops
