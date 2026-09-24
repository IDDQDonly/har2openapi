"""Command-line and interactive entry point."""

import argparse
import re
import sys

from har2openapi import har2openapi


def comma_list(value):
    return [item.strip() for item in value.split(',') if item.strip()] or None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Convert HAR files to OpenAPI 3.0 YAML.')
    parser.add_argument('filename', nargs='?', help='HAR file; omit for interactive mode')
    parser.add_argument('--url-filter', help='Domain (optionally with port), or regex starting with ^')
    parser.add_argument('--cookies', type=comma_list, help='Cookie names to keep, comma-separated')
    parser.add_argument('--ignore-headers', type=comma_list, help='Headers to omit, comma-separated; case-insensitive')
    parser.add_argument('-o', '--output-dir', default='.', help='Output directory (default: current directory)')
    parser.add_argument('--include-secrets', action='store_true', help='Disable masking of sensitive examples')
    parser.add_argument('--sensitive-names', type=comma_list, help='Additional field/header/query names to mask')
    args = parser.parse_args(argv)
    try:
        if not args.filename:
            args.filename = input('Enter the path to the HAR file:\n> ').strip()
            if args.url_filter is None:
                args.url_filter = input('Enter a domain or regex starting with ^ (or leave empty):\n> ').strip() or None
            if args.cookies is None:
                args.cookies = comma_list(input('Enter cookies to keep (comma-separated, or leave empty):\n> '))
            if args.ignore_headers is None:
                args.ignore_headers = comma_list(input('Enter headers to ignore (comma-separated, or leave empty):\n> '))
        converter = har2openapi(args.filename, url_filter=args.url_filter, cookie_filter=args.cookies,
                                ignore_headers=args.ignore_headers, output_dir=args.output_dir,
                                mask_secrets=not args.include_secrets, sensitive_names=args.sensitive_names)
        outputs = converter.create_openapi()
    except (OSError, ValueError, re.error, EOFError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    if not outputs:
        print('No matching requests; no files generated.')
        return 0
    for output in outputs:
        print(f'Created {output}')
    print('Done!')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
