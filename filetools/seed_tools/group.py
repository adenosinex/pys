# -*- coding: utf-8 -*-
def transform(files):
    """分组到新文件夹（每组 2 个）"""
    ops = []
    for i, f in enumerate(files):
        folder = "分组%03d" % (i // 2 + 1)
        name = f["path"].rsplit("/", 1)[-1]
        ops.append({"from": f["path"], "to": folder + "/" + name})
    return ops
