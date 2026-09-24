import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate
from openapi_schema_validator import OAS30Validator

from har2openapi import har2openapi


ROOT = Path(__file__).resolve().parents[1]
ABSENT = object()


def entry(url='https://example.com/items', method='GET', body=ABSENT, status=200,
          response_text='{"ok":true}', request_mime='application/json', response_mime='application/json'):
    request = {'url': url, 'method': method, 'headers': [], 'cookies': []}
    if body is not ABSENT:
        request['postData'] = {'mimeType': request_mime, 'text': json.dumps(body)}
    return {'request': request, 'response': {'status': status, 'content': {
        'mimeType': response_mime, 'text': response_text}}}


def convert(tmp_path, entries, **kwargs):
    source = tmp_path / 'input.har'
    source.write_text(json.dumps({'log': {'entries': entries}}))
    converter = har2openapi(source, output_dir=tmp_path / 'out', **kwargs)
    files = converter.create_openapi()
    docs = [yaml.safe_load(path.read_text()) for path in files]
    for doc in docs:
        validate(doc)
        for path in doc['paths'].values():
            for operation in path.values():
                for parameter in operation['parameters']:
                    OAS30Validator(parameter['schema']).validate(parameter['example'])
                bodies = [operation.get('requestBody', {}), *operation['responses'].values()]
                for body in bodies:
                    for media in body.get('content', {}).values():
                        if 'example' in media:
                            OAS30Validator(media['schema']).validate(media['example'])
    return docs


@pytest.mark.parametrize('body,kind', [({}, 'object'), ([], 'array'), (False, 'boolean'),
                                      (0, 'integer'), (0.5, 'number'), ('', 'string'), (None, 'string')])
def test_empty_and_scalar_json_bodies(tmp_path, body, kind):
    doc, = convert(tmp_path, [entry(method='POST', body=body, response_mime='text/plain', response_text='ok')])
    operation = doc['paths']['/items']['post']
    media = operation['requestBody']['content']['application/json']
    assert media['schema']['type'] == kind
    assert media['example'] == body
    assert 'text/plain' in operation['responses']['200']['content']
    if body is None:
        assert media['schema']['nullable'] is True


def test_no_body_root_path_and_empty_query(tmp_path):
    doc, = convert(tmp_path, [entry(url='https://example.com?empty=&tag=a&tag=b', status=204)])
    operation = doc['paths']['/']['get']
    assert 'requestBody' not in operation
    assert 'content' not in operation['responses']['204']
    params = {p['name']: p for p in operation['parameters']}
    assert params['empty']['example'] == ''
    assert params['tag']['example'] == ['a', 'b']
    assert params['tag']['schema']['type'] == 'array'
    assert not params['tag']['required']


def test_nested_and_mixed_json_types(tmp_path):
    body = {'items': [1, True, None, {'name': 'test'}, ['nested']], 'active': False}
    doc, = convert(tmp_path, [entry(method='POST', body=body, response_text=json.dumps(body))])
    operation = doc['paths']['/items']['post']
    request_schema = operation['requestBody']['content']['application/json']['schema']
    response_schema = operation['responses']['200']['content']['application/json']['schema']
    assert request_schema == response_schema
    assert len(request_schema['properties']['items']['items']['anyOf']) == 5


def test_masks_secrets_in_all_supported_locations(tmp_path):
    capture = entry(url='https://user:secretpass@example.com/items?access_token=querysecret&custom=customsecret',
                    method='POST', body={'nested': [{'password': 'bodysecret'}], 'count': 1},
                    response_text='{"refresh_token":"responsesecret"}')
    capture['request']['headers'] = [
        {'name': 'Authorization', 'value': 'Bearer authsecret'},
        {'name': 'X-API-Key', 'value': 'headersecret'},
        {'name': 'cookie', 'value': 'sid=cookiesecret; discard=dropsecret'}]
    capture['request']['cookies'] = [{'name': 'sid', 'value': 'cookiesecret'}]
    doc, = convert(tmp_path, [capture], cookie_filter=['sid'], sensitive_names=['custom'])
    serialized = json.dumps(doc)
    for secret in ['secretpass', 'querysecret', 'customsecret', 'bodysecret', 'responsesecret',
                   'authsecret', 'headersecret', 'cookiesecret', 'dropsecret']:
        assert secret not in serialized
    params = doc['paths']['/items']['post']['parameters']
    cookies = [p for p in params if p['in'] == 'cookie']
    assert len(cookies) == 1
    assert cookies[0]['name'] == 'sid'
    assert cookies[0]['example'] == '[REDACTED]'


def test_secret_opt_out_and_case_insensitive_ignore(tmp_path):
    capture = entry(url='https://example.com/items?token=visible')
    capture['request']['headers'] = [{'name': 'COOKIE', 'value': 'sid=hidden'},
                                     {'name': 'X-API-Key', 'value': 'visible-key'},
                                     {'name': 'uSeR-aGeNt', 'value': 'hidden-agent'}]
    capture['request']['cookies'] = [{'name': 'sid', 'value': 'hidden'}]
    doc, = convert(tmp_path, [capture], mask_secrets=False, ignore_headers=['Cookie', 'USER-AGENT'])
    params = doc['paths']['/items']['get']['parameters']
    assert {(p['name'], p['example']) for p in params} == {('token', 'visible'), ('x-api-key', 'visible-key')}


def test_cookie_filter_does_not_mutate_source_or_reintroduce_headers(tmp_path):
    capture = entry()
    capture['request']['cookies'] = [{'name': 'sid', 'value': 'one'}]
    capture['request']['headers'] = [{'name': 'Cookie', 'value': 'sid=two; other=three'}]
    source = tmp_path / 'input.har'
    source.write_text(json.dumps({'log': {'entries': [capture]}}))
    converter = har2openapi(source, output_dir=tmp_path / 'out', cookie_filter=['missing'])
    converter.create_openapi()
    assert converter.entries == [capture]
    doc = yaml.safe_load(next((tmp_path / 'out').glob('*.yaml')).read_text())
    assert not doc['paths']['/items']['get']['parameters']


def test_grouping_filter_and_response_merging(tmp_path):
    first = entry(method='POST', body={'id': 1})
    second = entry(method='POST', body=[1], status=201)
    third = entry(method='POST', body={'name': 'test'}, response_text='[1]')
    docs = convert(tmp_path, [first, second, third, entry(url='https://other.example.com')])
    assert len(docs) == 2
    operation = docs[0]['paths']['/items']['post']
    assert set(operation['responses']) == {'200', '201'}
    assert len(operation['requestBody']['content']['application/json']['schema']['anyOf']) == 3
    docs = convert(tmp_path, [first, entry(url='https://other.example.com')], url_filter='example.com')
    assert len(docs) == 1


def test_text_is_not_coerced_to_json(tmp_path):
    capture = entry(method='POST', body='placeholder', request_mime='text/plain',
                    response_mime='text/plain', response_text='false')
    capture['request']['postData']['text'] = '123'
    doc, = convert(tmp_path, [capture])
    op = doc['paths']['/items']['post']
    assert op['requestBody']['content']['text/plain']['example'] == '123'
    assert op['responses']['200']['content']['text/plain']['example'] == 'false'


def test_form_redaction_and_base64_response(tmp_path):
    capture = entry(method='POST', body={}, request_mime='application/x-www-form-urlencoded')
    capture['request']['postData']['text'] = 'password=secret&tag=a&tag=b&empty='
    capture['response']['content'] = {'mimeType': 'image/png', 'encoding': 'base64', 'text': 'c2VjcmV0'}
    doc, = convert(tmp_path, [capture])
    op = doc['paths']['/items']['post']
    example = op['requestBody']['content']['application/x-www-form-urlencoded']['example']
    assert example == {'password': '[REDACTED]', 'tag': ['a', 'b'], 'empty': ''}
    assert 'example' not in op['responses']['200']['content']['image/png']


@pytest.mark.parametrize('data', [{}, {'log': {'entries': {}}}, {'log': {'entries': [{}]}}])
def test_invalid_har(tmp_path, data):
    source = tmp_path / 'invalid.har'
    source.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Invalid HAR'):
        har2openapi(source).create_openapi()


def run_cli(*args, stdin=None):
    return subprocess.run([sys.executable, str(ROOT / 'main.py'), *map(str, args)],
                          input=stdin, text=True, capture_output=True)


def test_cli_and_interactive_mode(tmp_path):
    source = tmp_path / 'input.har'
    source.write_text(json.dumps({'log': {'entries': [entry()]}}))
    for interactive in [False, True]:
        output = tmp_path / str(interactive)
        args = ['-o', output] if interactive else [source, '--url-filter', 'example.com', '-o', output]
        result = run_cli(*args, stdin=f'{source}\n\n\n\n' if interactive else None)
        assert result.returncode == 0, result.stderr
        assert 'Done!' in result.stdout
        validate(yaml.safe_load(next(output.glob('*.yaml')).read_text()))


def test_cli_errors_and_no_matches(tmp_path):
    result = run_cli(tmp_path / 'missing.har')
    assert result.returncode == 1
    assert 'Traceback' not in result.stderr
    source = tmp_path / 'input.har'
    source.write_text(json.dumps({'log': {'entries': [entry()]}}))
    result = run_cli(source, '--url-filter', '^[', '-o', tmp_path / 'out')
    assert result.returncode == 1
    assert 'Traceback' not in result.stderr
    result = run_cli(source, '--url-filter', 'missing.example.com', '-o', tmp_path / 'out')
    assert result.returncode == 0
    assert 'No matching requests' in result.stdout
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('section,field,value', [
    ('request', 'headers', None),
    ('request', 'headers', [{'name': 'X-Test'}]),
    ('request', 'cookies', [{}]),
    ('request', 'postData', {'text': {}}),
    ('response', 'content', None),
    ('response', 'content', {'mimeType': 1}),
])
def test_malformed_entry_cli_reports_error(tmp_path, section, field, value):
    capture = entry()
    capture[section][field] = value
    source = tmp_path / 'invalid.har'
    source.write_text(json.dumps({'log': {'entries': [capture]}}))
    result = run_cli(source, '-o', tmp_path / 'out')
    assert result.returncode == 1
    assert 'Invalid HAR entry 0' in result.stderr
    assert 'Traceback' not in result.stderr


def test_regex_filter_is_reusable_and_does_not_mutate(tmp_path):
    captures = [entry(), entry(url='https://other.example.com/items')]
    converter = har2openapi('unused', url_filter=r'^https://example\.com/')
    assert converter.filter_urls(captures) == [captures[0]]
    assert converter.filter_urls(captures) == [captures[0]]
    assert converter.url_filter == r'^https://example\.com/'


def test_failed_status_and_head_response(tmp_path):
    doc, = convert(tmp_path, [entry(status=0), entry(method='HEAD')])
    assert 'default' in doc['paths']['/items']['get']['responses']
    assert 'content' not in doc['paths']['/items']['head']['responses']['200']


def test_cli_secret_controls(tmp_path):
    capture = entry(url='https://example.com?credential=private&token=secret')
    source = tmp_path / 'input.har'
    source.write_text(json.dumps({'log': {'entries': [capture]}}))
    for include in (False, True):
        output = tmp_path / str(include)
        args = [source, '-o', output, '--sensitive-names', 'credential']
        if include:
            args.append('--include-secrets')
        result = run_cli(*args)
        assert result.returncode == 0, result.stderr
        doc = yaml.safe_load(next(output.glob('*.yaml')).read_text())
        params = {p['name']: p['example'] for p in doc['paths']['/']['get']['parameters']}
        assert params == ({'credential': 'private', 'token': 'secret'} if include else
                          {'credential': '[REDACTED]', 'token': '[REDACTED]'})
