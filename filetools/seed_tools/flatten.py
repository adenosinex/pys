# -*- coding: utf-8 -*-
"""示例工具：把所有子文件夹里的文件集中到根目录（已在根目录的不动）。
重名由客户端执行时自动加后缀，plan 只给目标名。"""

META = {
    "id": "flatten",
    "name": "集中到根目录",
    "desc": "把子文件夹中的文件全部移到根目录下",
    "needs": [],
    "params": [],
}


def plan(files, params):
    ops = []
    for f in files:
        p = f["path"]
        if "/" in p:
            ops.append({"from": p, "to": p.rsplit("/", 1)[-1]})
    return ops
