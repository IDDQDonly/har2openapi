from har2openapi import har2openapi

def main():
    print("Enter the path to the HAR file:")
    filename = input("> ").strip()

    print("Enter a regular expression to filter URLs (or leave empty):")
    url_filter = input("> ").strip()
    url_filter = url_filter if url_filter else None

    print("Enter a list of cookies to filter (comma-separated, or leave empty):")
    cookie_filter = input("> ").strip()
    cookie_filter = [cookie.strip() for cookie in cookie_filter.split(',')] if cookie_filter else None

    print("Enter a list of headers to ignore (comma-separated, or leave empty):")
    ignore_headers = input("> ").strip()
    ignore_headers = [header.strip() for header in ignore_headers.split(',')] if ignore_headers else None

    har_converter = har2openapi(
        filename=filename,
        url_filter=url_filter,
        cookie_filter=cookie_filter,
        ignore_headers=ignore_headers
    )

    print("Generating OpenAPI schema...")
    har_converter.create_openapi()
    print("Done!")

if __name__ == "__main__":
    main()
