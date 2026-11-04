"""Extract one URL to stdout or a file, using only Python's standard library."""
import argparse
from pathlib import Path
import sys

if __package__:
    from .extractor import ZhihuError, extract, render
else:
    from extractor import ZhihuError, extract, render


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='知乎文章 / 回答 / 问题转 Markdown、纯文本或 JSON')
    parser.add_argument('url')
    parser.add_argument('--cookie-file', default='', help='Cookie 请求头文件，不传时使用环境变量')
    parser.add_argument('--format', choices=['markdown', 'text', 'json'], default='markdown')
    parser.add_argument('--no-images', action='store_true')
    parser.add_argument('-o', '--output', help='结果文件；不传则输出到终端')
    args = parser.parse_args()
    try:
        doc = extract(args.url, cookie_file=args.cookie_file, keep_images=not args.no_images)
        content = render(doc, args.format)
        if args.output:
            Path(args.output).write_text(content, encoding='utf-8')
        else:
            print(content)
        return 0
    except (ZhihuError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
