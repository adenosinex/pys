# -*- coding: utf-8 -*-
def transform(files):
    """集中到根目录"""
    ops = []
    for f in files:
        p = f["path"]
        if "/" in p:
            ops.append({"from": p, "to": p.rsplit("/", 1)[-1]})
    return ops
