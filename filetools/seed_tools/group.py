# -*- coding: utf-8 -*-
def transform(path, index):
    """分组到新文件夹（每组 2 个）"""
    folder = "分组%03d" % (index // 2 + 1)
    return folder + "/" + path.rsplit("/", 1)[-1]
