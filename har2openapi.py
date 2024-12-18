import json
import yaml
from urllib.parse import urlparse, parse_qs
from collections import defaultdict
import re
from typing import List, Dict, Optional, Union


class har2openapi:
    def __init__(self, filename: str, url_filter: Optional[Union[str, List[str]]] = None,
                 cookie_filter: Optional[List[str]] = None, ignore_headers: Optional[List[str]] = None) -> None:
        """
        :param filename: Path to the HAR file.
        :param url_filter: List or regular expression for filtering URLs.
        :param cookie_filter: List of cookie names to keep.
        :param ignore_headers: List of headers to ignore.
        """
        self.filename = filename
        self.url_filter = url_filter
        self.cookie_filter = cookie_filter
        self.ignore_headers = ignore_headers or []

    def open_file(self) -> None:
        """Opens and loads data from the HAR file."""
        with open(self.filename, "r", encoding='UTF-8') as f:
            self.har_data = json.load(f)
            self.entries = self.har_data['log']['entries']

    def write_to_file(self, data: Dict, filename: str) -> None:
        """Writes data to a YAML file."""
        with open(filename, "w", encoding='UTF-8') as f:
            yaml.dump(data, f, default_flow_style=False)

    def filter_urls(self, entries: List[Dict]) -> List[Dict]:
        """Filters URLs based on the provided filter."""
        if not self.url_filter:
            return entries

        # Automatically convert a simple domain to a regular expression
        if isinstance(self.url_filter, str):
            if not self.url_filter.startswith("^"):
                self.url_filter = rf"^https?://{re.escape(self.url_filter)}/.*"
            regex = re.compile(self.url_filter)
            return [entry for entry in entries if regex.search(entry['request']['url'])]

        if isinstance(self.url_filter, list):
            return [entry for entry in entries if any(
                re.search(rf"^https?://{re.escape(url)}/.*", entry['request']['url']) for url in self.url_filter)]

        return entries

    def filter_cookies(self, cookies: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Keeps only unique cookies that match the filter."""
        if not self.cookie_filter:
            return cookies

        # Use a set to ensure unique cookies
        unique_cookies = {cookie['name']: cookie for cookie in cookies if cookie['name'] in self.cookie_filter}
        return list(unique_cookies.values())

    def filter_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Removes headers specified in the ignore_headers list."""
        return {k: v for k, v in headers.items() if k not in self.ignore_headers}

    def format_cookies(self, cookies: List[Dict[str, str]]) -> str:
        """Formats cookies as 'name=value; name2=value2'."""
        return '; '.join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)

    def generate_parameters(self, query_params: Dict[str, List[str]], headers: Dict[str, str]) -> List[Dict[str, Union[str, Dict]]]:
        """Generates query and header parameters for OpenAPI."""
        parameters = []

        # Add query string parameters
        for param, values in query_params.items():
            parameters.append({
                "name": param,
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
                "example": values[0] if values else None
            })

        # Add header parameters
        for header, value in headers.items():
            parameters.append({
                "name": header,
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
                "example": value
            })

        return parameters

    def parse_post_data(self, post_data: Optional[str]) -> Union[str, dict, None]:
        """Converts a string to JSON if necessary."""
        if isinstance(post_data, str):
            try:
                return json.loads(post_data)  # Convert string to JSON
            except json.JSONDecodeError:
                return post_data  # Return as-is if parsing fails
        return post_data  # Return as-is if it's not a string

    def parse_cookie_string(self, cookie_string: str) -> List[Dict[str, str]]:
        """Parses a cookie string into a list of cookies."""
        cookies = []
        for cookie in cookie_string.split(';'):
            cookie = cookie.strip()
            if '=' in cookie:
                name, value = cookie.split('=', 1)
                cookies.append({'name': name, 'value': value})
        return cookies

    def generate_request_body(self, mime_type: str, body: Optional[Union[str, dict]]) -> Optional[Dict[str, dict]]:
        """
        Generates the request body for OpenAPI. The body is always marked as required.
        :param mime_type: MIME type of the request body.
        :param body: Request body data.
        :return: Dictionary describing requestBody.
        """
        if body:
            return {
                "required": True,  # Mark requestBody as required
                "content": {
                    mime_type: {
                        "schema": {"type": "string"},
                        "example": body
                    }
                }
            }

    def generate_response_body(self, mime_type: str, response_body: str) -> Optional[Dict[str, dict]]:
        """Generates the response body for OpenAPI."""
        if response_body:
            return {
                "content": {
                    mime_type: {
                        "schema": {"type": "string"},
                        "example": response_body
                    }
                }
            }
        return None

    def create_openapi(self) -> None:
        """Generates OpenAPI specifications from the HAR file."""
        self.open_file()

        # Filter entries by URL
        self.entries = self.filter_urls(self.entries)

        # Group entries by base URL
        grouped_entries = defaultdict(list)
        for entry in self.entries:
            url = entry['request']['url']
            base_url = urlparse(url)._replace(path='', params='', query='', fragment='').geturl()
            grouped_entries[base_url].append(entry)

        # Create an OpenAPI schema for each base URL
        for base_url, entries in grouped_entries.items():
            paths = {}
            for entry in entries:
                url = entry['request']['url']
                parsed_url = urlparse(url)
                method = entry['request']['method'].lower()
                headers = {header['name']: header['value'] for header in entry['request'].get('headers', [])}
                headers = self.filter_headers(headers)

                body = entry['request'].get('postData', {}).get('text', None)
                body = self.parse_post_data(body)

                cookies = entry['request'].get('cookies', [])
                cookie_header = headers.get('Cookie', '')
                if cookie_header:
                    cookies.extend(self.parse_cookie_string(cookie_header))
                cookies = self.filter_cookies(cookies)

                if cookies:
                    headers['Cookie'] = self.format_cookies(cookies)

                path = parsed_url.path
                query_params = parse_qs(parsed_url.query)

                status = entry['response'].get('status', 200)
                if not isinstance(status, int) or not (100 <= status <= 599):
                    status = 200

                mime_type = entry['response']['content'].get('mimeType', 'text/plain')
                response_body = entry['response']['content'].get('text', '')

                if path not in paths:
                    paths[path] = {}

                if method not in paths[path]:
                    paths[path][method] = {
                        "summary": f"Generated operation for {url}",
                        "parameters": self.generate_parameters(query_params, headers),
                        "requestBody": self.generate_request_body(mime_type, body),
                        "responses": {}
                    }

                status_str = str(status)
                response_data = {"description": f"Response for status {status}"}
                response_body_data = self.generate_response_body(mime_type, response_body)
                response_data.update(response_body_data if response_body_data else {})

                paths[path][method]["responses"][status_str] = response_data

            openapi_schema = {
                "openapi": "3.0.0",
                "info": {
                    "title": f"OpenAPI schema for {base_url}",
                    "version": "1.0.0",
                    "description": f"Auto-generated schema for {base_url}"
                },
                "servers": [{"url": base_url, "description": "Base URL from HAR file"}],
                "paths": paths
            }

            filename = f"openapi_{base_url.replace('://', '_').replace('/', '_')}.yaml"
            self.write_to_file(openapi_schema, filename)
