# -*- coding: utf-8 -*-
def transform(path):
    """集中到根目录"""
    if "/" in path:
        return path.rsplit("/", 1)[-1]
    return None
