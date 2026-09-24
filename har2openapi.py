"""Convert captured HTTP traffic into an OpenAPI 3.0 document per origin."""

import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml


MISSING = object()
HTTP_METHODS = {'get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace'}


def infer_schema(value):
    """Infer observed types without claiming fields are required."""
    if value is None:
        return {'type': 'string', 'nullable': True}
    if isinstance(value, bool):
        return {'type': 'boolean'}
    if isinstance(value, int):
        return {'type': 'integer'}
    if isinstance(value, float):
        return {'type': 'number'}
    if isinstance(value, dict):
        return {'type': 'object', 'properties': {k: infer_schema(v) for k, v in value.items()}}
    if isinstance(value, list):
        variants = []
        for item in value:
            schema = infer_schema(item)
            if schema not in variants:
                variants.append(schema)
        items = variants[0] if len(variants) == 1 else {'anyOf': variants} if variants else {}
        return {'type': 'array', 'items': items}
    return {'type': 'string'}


class har2openapi:
    def __init__(self, filename, url_filter=None, cookie_filter=None, ignore_headers=None,
                 output_dir='.', mask_secrets=True, sensitive_names=None):
        self.filename = filename
        self.url_filter = url_filter
        self.cookie_filter = cookie_filter
        self.ignore_headers = {name.lower() for name in (ignore_headers or [])}
        self.output_dir = Path(output_dir)
        self.mask_secrets = mask_secrets
        self.sensitive_names = {self._normalize(name) for name in (sensitive_names or [])}

    @staticmethod
    def _normalize(name):
        return re.sub(r'[^a-z0-9]', '', name.lower())

    def _is_sensitive(self, name):
        name = self._normalize(name)
        return (name in self.sensitive_names or name in {'authorization', 'proxyauthorization',
                'cookie', 'setcookie', 'password', 'passwd', 'secret', 'session', 'sessionid', 'sid'}
                or name.endswith(('token', 'apikey', 'secret', 'password')))

    def _redact(self, value, name='', force=False):
        if not self.mask_secrets:
            return value
        if force or self._is_sensitive(name):
            # Keep the example compatible with its inferred schema.
            if value is None:
                return None
            if isinstance(value, bool):
                return False
            if isinstance(value, (int, float)):
                return 0
            if isinstance(value, dict):
                return {}
            if isinstance(value, list):
                return []
            return '[REDACTED]'
        if isinstance(value, dict):
            return {k: self._redact(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v, name) for v in value]
        return value

    def open_file(self):
        with open(self.filename, encoding='utf-8') as source:
            self.har_data = json.load(source)
        self.load_data(self.har_data)

    def load_data(self, data):
        """Validate an in-memory capture for both CLI and local web imports."""
        self.har_data = data
        try:
            self.entries = self.har_data['log']['entries']
        except (KeyError, TypeError) as exc:
            raise ValueError('Invalid HAR: expected log.entries.') from exc
        if not isinstance(self.entries, list):
            raise ValueError('Invalid HAR: log.entries must be a list.')
        for index, entry in enumerate(self.entries):
            try:
                request, response = entry['request'], entry['response']
                parsed = urlparse(request['url'])
                if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
                    raise ValueError
                if request['method'].lower() not in HTTP_METHODS:
                    raise ValueError
                if not isinstance(response, dict):
                    raise ValueError
                for field in ('headers', 'cookies'):
                    values = request.get(field, [])
                    if not isinstance(values, list):
                        raise ValueError
                    for value in values:
                        if (not isinstance(value, dict) or not isinstance(value.get('name'), str)
                                or not value['name'] or not isinstance(value.get('value'), str)):
                            raise ValueError
                for container in (request.get('postData', {}), response.get('content', {})):
                    if not isinstance(container, dict):
                        raise ValueError
                    if 'text' in container and not isinstance(container['text'], str):
                        raise ValueError
                    if 'mimeType' in container and not isinstance(container['mimeType'], str):
                        raise ValueError
                # Accessing port also validates malformed port numbers.
                parsed.port
            except (KeyError, TypeError, AttributeError, ValueError) as exc:
                raise ValueError(
                    f'Invalid HAR entry {index}: check the HTTP URL, method, headers, cookies and body fields.'
                ) from exc

    def write_to_file(self, data, filename):
        with open(filename, 'w', encoding='utf-8') as target:
            yaml.safe_dump(data, target, sort_keys=False, allow_unicode=True)

    def filter_urls(self, entries):
        if not self.url_filter:
            return entries
        if isinstance(self.url_filter, str) and self.url_filter.startswith('^'):
            regex = re.compile(self.url_filter)
            return [e for e in entries if regex.search(e['request']['url'])]
        domains = [self.url_filter] if isinstance(self.url_filter, str) else self.url_filter
        domains = {domain.lower() for domain in domains}
        return [e for e in entries if urlparse(e['request']['url']).netloc.lower() in domains]

    def filter_cookies(self, cookies):
        return list({c['name']: dict(c) for c in cookies
                     if not self.cookie_filter or c['name'] in self.cookie_filter}.values())

    def filter_headers(self, headers):
        return {k: v for k, v in headers.items() if k.lower() not in self.ignore_headers}

    def format_cookies(self, cookies):
        return '; '.join(f"{c['name']}={c['value']}" for c in cookies)

    def generate_parameters(self, query_params, headers):
        parameters = []
        for name, values in query_params.items():
            example = values if len(values) > 1 else values[0] if values else ''
            parameter = {'name': name, 'in': 'query', 'required': False,
                         'schema': infer_schema(example), 'example': self._redact(example, name)}
            if isinstance(example, list):
                parameter.update(style='form', explode=True)
            parameters.append(parameter)
        # These headers have dedicated OpenAPI representations or are transport details.
        excluded = {'cookie', 'authorization', 'proxy-authorization', 'content-type', 'accept',
                    'host', 'content-length'}
        for name, value in headers.items():
            if name.lower() not in excluded and not name.startswith(':'):
                parameters.append({'name': name, 'in': 'header', 'required': False,
                                   'schema': {'type': 'string'}, 'example': self._redact(value, name)})
        return parameters

    def parse_post_data(self, post_data):
        if isinstance(post_data, str):
            try:
                return json.loads(post_data)
            except ValueError:
                pass
        return post_data

    def parse_cookie_string(self, cookie_string):
        cookies = []
        for item in cookie_string.split(';'):
            if '=' in item:
                name, value = item.strip().split('=', 1)
                cookies.append({'name': name, 'value': value})
        return cookies

    def _media(self, mime_type, body):
        mime_type = (mime_type or 'text/plain').split(';', 1)[0].strip().lower()
        if mime_type == 'application/json' or mime_type.endswith('+json'):
            body = self.parse_post_data(body)
        elif mime_type == 'application/x-www-form-urlencoded' and isinstance(body, str):
            body = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(body, keep_blank_values=True).items()}
        return {mime_type: {'schema': infer_schema(body), 'example': self._redact(body)}}

    def generate_request_body(self, mime_type, body=MISSING):
        if body is MISSING:
            return None
        return {'required': False, 'content': self._media(mime_type, body)}

    def generate_response_body(self, mime_type, response_body=MISSING):
        if response_body is MISSING:
            return None
        return {'content': self._media(mime_type, response_body)}

    @staticmethod
    def _merge_content(target, incoming):
        for mime, media in incoming.items():
            if mime not in target:
                target[mime] = media
                continue
            old = target[mime]['schema']
            new = media['schema']
            if old != new:
                variants = old.get('anyOf', [old]).copy()
                if new not in variants:
                    variants.append(new)
                target[mime]['schema'] = {'anyOf': variants}
            target[mime]['example'] = media['example']

    def build_documents(self):
        """Build documents from validated entries without writing to disk."""
        grouped = defaultdict(list)
        for entry in self.filter_urls(self.entries):
            parsed = urlparse(entry['request']['url'])
            host = parsed.hostname
            if ':' in host:
                host = f'[{host}]'
            port = f':{parsed.port}' if parsed.port else ''
            grouped[f'{parsed.scheme}://{host}{port}'].append(entry)
        documents = {}
        for base_url, entries in grouped.items():
            paths = {}
            for entry in entries:
                request, response = entry['request'], entry['response']
                parsed = urlparse(request['url'])
                path = parsed.path or '/'
                if parsed.params:
                    path += ';' + parsed.params
                # Literal braces in captured URLs are not OpenAPI path templates.
                path = path.replace('{', '%7B').replace('}', '%7D')
                method = request['method'].lower()
                headers = self.filter_headers({h['name'].lower(): h['value'] for h in request.get('headers', [])})
                parameters = self.generate_parameters(parse_qs(parsed.query, keep_blank_values=True), headers)
                if 'cookie' not in self.ignore_headers:
                    cookies = list(request.get('cookies', []))
                    cookies.extend(self.parse_cookie_string(headers.get('cookie', '')))
                    for cookie in self.filter_cookies(cookies):
                        parameters.append({'name': cookie['name'], 'in': 'cookie', 'required': False,
                                           'schema': {'type': 'string'},
                                           'example': self._redact(cookie['value'], force=True)})
                operation = paths.setdefault(path, {}).setdefault(method, {
                    'summary': f'Generated {method.upper()} operation', 'parameters': [], 'responses': {}})
                known = {(p['in'], p['name']) for p in operation['parameters']}
                operation['parameters'].extend(p for p in parameters if (p['in'], p['name']) not in known)
                post_data = request.get('postData', {})
                body = post_data.get('text', MISSING)
                request_body = self.generate_request_body(
                    post_data.get('mimeType') or headers.get('content-type', 'application/octet-stream'), body)
                if request_body is not None:
                    existing = operation.setdefault('requestBody', {'required': False, 'content': {}})
                    self._merge_content(existing['content'], request_body['content'])
                status = response.get('status')
                status_key = str(status) if type(status) is int and 100 <= status <= 599 else 'default'
                response_data = operation['responses'].setdefault(status_key, {
                    'description': f'Response for status {status_key}'})
                content = response.get('content', {})
                response_text = content.get('text', MISSING)
                if status not in {204, 304} and method != 'head' and response_text is not MISSING:
                    # Encoded binary payloads are represented without an unusable text example.
                    if content.get('encoding') == 'base64':
                        media = {content.get('mimeType') or 'application/octet-stream': {'schema': {'type': 'string', 'format': 'byte'}}}
                        response_data.setdefault('content', {}).update(media)
                    else:
                        response_body = self.generate_response_body(content.get('mimeType'), response_text)
                        self._merge_content(response_data.setdefault('content', {}), response_body['content'])
            schema = {'openapi': '3.0.3', 'info': {'title': f'OpenAPI schema for {base_url}', 'version': '1.0.0'},
                      'servers': [{'url': base_url}], 'paths': paths}
            documents[base_url] = schema
        return documents

    def create_openapi(self):
        """Write one YAML per origin and return the generated paths."""
        self.open_file()
        outputs = []
        for base_url, schema in self.build_documents().items():
            self.output_dir.mkdir(parents=True, exist_ok=True)
            filename = 'openapi_' + re.sub(r'[^a-zA-Z0-9._-]', '_', base_url.replace('://', '_')) + '.yaml'
            output = self.output_dir / filename
            self.write_to_file(schema, output)
            outputs.append(output)
        return outputs
