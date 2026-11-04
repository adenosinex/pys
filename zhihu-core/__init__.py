"""Standalone, standard-library-only Zhihu extraction core."""
from .extractor import Doc, ZhihuError, extract, extract_html

__all__ = ['Doc', 'ZhihuError', 'extract', 'extract_html']
