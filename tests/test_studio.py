from copy import deepcopy
from io import BytesIO
import json
import time
from zipfile import ZipFile

from openapi_spec_validator import validate
from openapi_schema_validator import OAS30Validator
import pytest
import yaml

from studio.analysis import analyze, compare, parse_capture, route_key, validate_template
from studio.web import create_app


def har(*rows):
    entries = []
    for row in rows:
        path, payload = row[:2]
        status = row[2] if len(row) > 2 else 200
        entries.append({'request': {'url': 'https://api.example.com' + path, 'method': 'GET',
                                    'headers': [{'name': 'Authorization', 'value': 'Bearer top-secret'}],
                                    'cookies': [{'name': 'sid', 'value': 'cookie-secret'}]},
                        'response': {'status': status, 'content': {'mimeType': 'application/json',
                                                                 'text': json.dumps(payload)}}})
    return json.dumps({'log': {'entries': entries}}).encode()


@pytest.fixture
def workspace(tmp_path):
    app = create_app(tmp_path)
    app.config['TESTING'] = True
    client = app.test_client()
    client.get('/')
    with client.session_transaction() as session:
        token = session['csrf']
    yield app, client, token
    app.extensions['jobs'].shutdown()


def wait_job(client, location):
    identifier = location.split('/')[2]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.application.extensions['store'].job(identifier)
        if job['state'] in {'done', 'failed'}:
            return job
        time.sleep(0.01)
    raise AssertionError('Background job did not finish')


def post(client, token, url, data=None):
    response = client.post(url, data={'csrf': token, **(data or {})})
    if response.status_code == 302 and response.location.startswith('/jobs/'):
        wait_job(client, response.location)
        response = client.get(response.location)
    return response


def ready_get(client, url, **kwargs):
    response = client.get(url, **kwargs)
    for _ in range(10):
        if response.status_code != 302:
            return response
        if response.location.startswith('/jobs/'):
            wait_job(client, response.location)
        response = client.get(response.location)
    raise AssertionError('Too many analysis redirects')


def new_project(client, token):
    response = post(client, token, '/projects', {'name': 'Магазин'})
    assert response.status_code == 302
    return response.location.split('/')[-1]


def upload(client, token, project, raw, **kwargs):
    response = post(client, token, f'/projects/{project}/import',
                    {'files': (BytesIO(raw), 'capture.har'), **kwargs})
    assert response.status_code == 302, response.text
    return response.location.split('/')[2]


def select(client, token, draft, revision=0):
    response = post(client, token, f'/drafts/{draft}/domains',
                    {'domains': 'https://api.example.com', 'revision': revision})
    assert response.status_code == 302, response.text


def save(client, token, draft, revision, name='Версия 1'):
    response = post(client, token, f'/drafts/{draft}/save', {'revision': revision, 'name': name})
    assert response.status_code == 302, response.text
    return response.location.split('/')[-1]


def test_evidence_counts_and_structural_schema_merge():
    observations = parse_capture(har(('/users/1', {'id': 1, 'email': 'a'}),
                                     ('/users/2', {'id': 2}), ('/users/me', {'id': 9})), 'one.har')
    report = analyze(observations, {})
    assert len(report['suggestions']) == 1
    suggestion = report['suggestions'][0]
    assert suggestion['template'] == '/users/{user_id}'
    assert '/users/me' not in suggestion['paths']
    report = analyze(observations, {suggestion['key']: suggestion['template']})
    assert len(report['operations']) == 2
    operation = next(o for o in report['operations'] if '{' in o['path'])
    email = next(f for f in operation['fields'] if f['name'] == '$/email')
    assert email['count'] == 1 and email['total'] == 2
    assert email['sources'] == [observations[0]['id']]
    body = operation['schema']['responses']['200']['content']['application/json']
    assert body['schema']['type'] == 'object'
    assert set(body['schema']['properties']) == {'id', 'email'}
    assert 'required' not in body['schema']
    validate(next(iter(report['documents'].values())))
    OAS30Validator(body['schema']).validate(body['example'])


def test_masks_before_persistence_and_preserves_field_type_evidence():
    observations = parse_capture(har(('/users/1', {'password': 'body-secret', 'secret': {'nested': 42}})), 'a.har')
    serialized = json.dumps(observations)
    for secret in ('top-secret', 'cookie-secret', 'body-secret'):
        assert secret not in serialized
    assert '42' not in json.dumps(observations[0]['operation'])
    fields = observations[0]['fields']['response 200 · application/json']
    assert fields['$/secret/nested'] == ['integer']


def test_full_import_review_save_extend_compare_export(workspace):
    app, client, token = workspace
    project = new_project(client, token)
    raw = har(('/users/1', {'id': 1, 'email': 'first'}), ('/users/2', {'id': 2}), ('/users/me', {'id': 3}))
    draft = upload(client, token, project, raw)
    domains = ready_get(client, f'/drafts/{draft}/domains')
    assert domains.status_code == 200
    assert 'api.example.com' in domains.text
    select(client, token, draft)
    result = ready_get(client, f'/drafts/{draft}')
    assert result.status_code == 200, result.text
    store = app.extensions['store']
    data = store.get('drafts', draft)['data']
    proposal = analyze(data['observations'], {})['suggestions'][0]
    response = post(client, token, f'/drafts/{draft}/rules',
                    {'key': proposal['key'], 'template': '/users/{id}', 'choice': 'accept', 'revision': 1})
    assert response.status_code == 302
    version = save(client, token, draft, 2)
    snapshot = deepcopy(store.get('versions', version))
    result = ready_get(client, f'/versions/{version}')
    assert result.status_code == 200, result.text
    assert '/users/{id}' in result.text
    assert not store.drafts(project)
    assert store.project(project)['rules'][proposal['key']] == '/users/{id}'
    report = analyze(snapshot['data']['observations'], snapshot['data']['rules'])
    operation = next(o for o in report['operations'] if '{id}' in o['path'])
    evidence = ready_get(client, f'/versions/{version}/evidence', query_string={
        'operation': operation['id'], 'context': 'response 200 · application/json', 'field': '$/email'})
    assert evidence.status_code == 200
    assert 'Запись №1' in evidence.text
    assert 'Запись №2' not in evidence.text
    # Repeat import is a no-op for counts, but creates a new immutable snapshot.
    extension = upload(client, token, project, raw, mode='extend', base_id=version)
    select(client, token, extension)
    extended = store.get('drafts', extension)
    assert len(extended['data']['observations']) == 3
    assert not analyze(extended['data']['observations'], extended['data']['rules'])['suggestions']
    save(client, token, extension, 1, 'Дополненная версия')
    # New IDs inherit the route rule. Comparison does not include old observations.
    new_raw = har(('/users/99', {'id': '99'}), ('/users/100', {'id': '100'}, 409))
    comparison = upload(client, token, project, new_raw, mode='compare', base_id=version)
    select(client, token, comparison)
    page = ready_get(client, f'/drafts/{comparison}')
    assert page.status_code == 200
    assert 'integer → string' in page.text
    assert 'удаление не подтверждено' in page.text
    assert '409' in page.text
    assert len(store.get('drafts', comparison)['data']['observations']) == 2
    version2 = save(client, token, comparison, 1, 'Релиз 2')
    assert store.get('versions', version) == snapshot
    diff = ready_get(client, f'/projects/{project}/compare', query_string={'before': version, 'after': version2})
    assert diff.status_code == 200, diff.text
    assert 'integer → string' in diff.text
    exported = ready_get(client, f'/versions/{version2}/export')
    assert exported.status_code == 200, exported.text
    with ZipFile(BytesIO(exported.data)) as archive:
        assert {'report.html', 'evidence.json', 'openapi-1.yaml'} <= set(archive.namelist())
        validate(yaml.safe_load(archive.read('openapi-1.yaml')))
        assert 'Релиз 2' in archive.read('report.html').decode()
        assert 'top-secret' not in archive.read('evidence.json').decode()


def test_multiple_files_and_duplicate_upload(workspace):
    app, client, token = workspace
    project = new_project(client, token)
    a, b = har(('/items/1', {'id': 1})), har(('/items/2', {'id': 2}))
    response = post(client, token, f'/projects/{project}/import', {'files': [
        (BytesIO(a), 'one.har'), (BytesIO(b), 'two.har'), (BytesIO(a), 'copy.har')]})
    assert response.status_code == 302
    identifier = response.location.split('/')[2]
    observations = app.extensions['store'].get('drafts', identifier)['data']['observations']
    assert len(observations) == 2
    assert analyze(observations, {})['files'] == 2


def test_rejected_rule_applies_to_future_ids():
    observations = parse_capture(har(('/items/1', {}), ('/items/2', {})), 'a.har')
    key = route_key(observations[0])
    report = analyze(observations, {key: None})
    assert not report['suggestions']
    assert len(report['operations']) == 2
    new = parse_capture(har(('/items/3', {}), ('/items/4', {})), 'b.har')
    assert not analyze(new, {key: None})['suggestions']


@pytest.mark.parametrize('template', ['/orders/{id}', '/users/{bad-name}', '/users/id', '/users/{id}/extra'])
def test_invalid_route_edits(template):
    key = json.dumps(['https://api.example.com', 'get', '/users/{}'])
    with pytest.raises(ValueError):
        validate_template(key, template)


def test_sample_absence_is_not_called_a_breaking_change():
    old = analyze(parse_capture(har(('/items', {'price': 1, 'email': 'a'})), 'old.har'), {})
    new = analyze(parse_capture(har(('/items', {'price': '1'})), 'new.har'), {})
    changes = compare(old, new)
    assert len(changes) == 2
    assert {c['kind'] for c in changes} == {'type', 'unobserved'}
    assert 'удаление не подтверждено' in next(c['message'] for c in changes if c['kind'] == 'unobserved')


def test_security_invalid_import_and_persistence(workspace, tmp_path):
    app, client, token = workspace
    project = new_project(client, token)
    assert client.post('/projects', data={'name': 'CSRF'}).status_code == 400
    assert client.get('/', headers={'Host': 'untrusted.example'}).status_code == 400
    response = post(client, token, f'/projects/{project}/import', {'files': (BytesIO(b'not json'), 'bad.har')})
    assert response.status_code == 200
    assert 'bad.har' in response.text
    assert not app.extensions['store'].drafts(project)
    reloaded = create_app(tmp_path)
    assert reloaded.extensions['store'].project(project)['name'] == 'Магазин'
    draft = upload(client, token, project, har(('/items', {'name': '<script>alert(1)</script>'})))
    select(client, token, draft)
    assert '<script>alert(1)</script>' not in ready_get(client, f'/drafts/{draft}').text
    # Stale tabs cannot overwrite newer draft changes.
    stale = post(client, token, f'/drafts/{draft}/domains', {'domains': 'https://api.example.com', 'revision': 0})
    assert stale.status_code == 409
    app.config['MAX_CONTENT_LENGTH'] = 10
    large = client.post('/projects', data={'csrf': token, 'name': 'Too much'})
    assert large.status_code == 413


def test_project_version_isolation(workspace):
    app, client, token = workspace
    project1 = new_project(client, token)
    draft = upload(client, token, project1, har(('/items', {})))
    select(client, token, draft)
    version = save(client, token, draft, 1)
    project2 = new_project(client, token)
    response = post(client, token, f'/projects/{project2}/import', {'mode': 'extend', 'base_id': version,
                                                                'files': (BytesIO(har(('/a', {}))), 'a.har')})
    assert response.status_code == 400
    assert not app.extensions['store'].drafts(project2)


def test_new_status_is_one_change_with_matching_evidence():
    old = analyze(parse_capture(har(('/items', {'id': 1})), 'old.har'), {})
    new = analyze(parse_capture(har(('/items', {'id': 2}), ('/items', {'code': 'LOCKED', 'message': 'Locked'}, 409)), 'new.har'), {})
    changes = compare(old, new)
    assert len(changes) == 1
    assert changes[0]['message'] == 'Впервые наблюдался ответ 409.'
    assert not changes[0]['before_sources']
    assert changes[0]['after_sources'][0]['index'] == 2


def test_mixed_array_and_nullable_schema_examples_validate():
    observations = parse_capture(har(('/items', [{'id': 1, 'tag': None}, {'id': 2}]),
                                     ('/items', [{'id': 'a', 'tag': 'text', 'extra': True}])), 'mixed.har')
    report = analyze(observations, {})
    document = next(iter(report['documents'].values()))
    validate(document)
    schema = report['operations'][0]['schema']['responses']['200']['content']['application/json']['schema']
    for observation in observations:
        value = observation['operation']['responses']['200']['content']['application/json']['example']
        OAS30Validator(schema).validate(value)
    field = next(f for f in report['operations'][0]['fields'] if f['name'] == '$/*/id')
    assert field['count'] == 2
    assert field['types'] == {'integer': 1, 'string': 1}


def test_reset_rule_is_saved_and_snapshots_remain_unchanged(workspace):
    app, client, token = workspace
    project = new_project(client, token)
    raw = har(('/users/1', {}), ('/users/2', {}))
    draft = upload(client, token, project, raw)
    select(client, token, draft)
    store = app.extensions['store']
    key = route_key(store.get('drafts', draft)['data']['observations'][0])
    post(client, token, f'/drafts/{draft}/rules',
         {'key': key, 'template': '/users/{id}', 'choice': 'accept', 'revision': 1})
    version = save(client, token, draft, 2)
    draft2 = upload(client, token, project, raw, mode='extend', base_id=version)
    select(client, token, draft2)
    response = post(client, token, f'/drafts/{draft2}/rules/reset', {'key': key, 'revision': 1})
    assert response.status_code == 302
    save(client, token, draft2, 2, 'Пересмотренная версия')
    assert key not in store.project(project)['rules']
    assert store.get('versions', version)['data']['rules'][key] == '/users/{id}'


def test_field_overview_groups_bodies_and_hides_only_structural_nodes():
    observations = parse_capture(har(
        ('/users', {'users': [{'id': 1, 'email': 'one'}, {'id': 2}], 'empty': []}),
        ('/users', {'users': [{'id': 3}], 'empty': []}),
        ('/users', {'error': 'missing'}, 404),
    ), 'users.har')
    operation = analyze(observations, {})['operations'][0]
    success, error = operation['field_groups']
    assert (success['status'], success['total']) == ('200', 2)
    assert (error['status'], error['total']) == ('404', 1)
    fields = {f['label']: f for f in success['fields']}
    assert set(fields) == {'users[].id', 'users[].email', 'empty'}
    assert fields['users[].email']['count'] == 1
    assert fields['users[].email']['total'] == 2
    assert fields['users[].email']['in_array']
    assert len(fields['users[].email']['sources']) == 1
    assert {'$', '$/users', '$/users/*'} <= {f['name'] for f in operation['fields']}


def test_field_overview_preserves_mixed_types_root_values_and_escaped_names():
    observations = parse_capture(har(
        ('/mixed', {'value': {'id': 1}, 'a/b': {'x.y': None}}),
        ('/mixed', {'value': 'text'}),
    ), 'mixed.har')
    fields = analyze(observations, {})['operations'][0]['field_groups'][0]['fields']
    labels = {f['label']: f for f in fields}
    assert set(labels['value']['types']) == {'object', 'string'}
    assert '["a/b"]["x.y"]' in labels
    scalar = analyze(parse_capture(har(('/scalar', 0)), 'scalar.har'), {})
    assert scalar['operations'][0]['field_groups'][0]['fields'][0]['label'] == ''


def test_field_overview_template_keeps_evidence_and_explains_array_denominator(workspace):
    app, client, token = workspace
    observations = parse_capture(har(('/users', {'users': [{'email': 'one'}, {}]})), 'users.har')
    report = analyze(observations, {})
    from flask import render_template
    with app.test_request_context('/'):
        rendered = render_template('operations.html', report=report, standalone=False,
                                   draft=True, record={'id': 'example'})
    assert 'users[].email' in rendered
    assert 'Ответов с полем' in rendered
    assert '1 из 100' in rendered
    assert '<code>$</code>' not in rendered
    assert 'field=' in rendered
    with app.test_request_context('/'):
        from flask import session
        session['language'] = 'en'
        rendered = render_template('operations.html', report=report, standalone=True)
    assert 'Responses containing field' in rendered
    assert 'in at least one item' in rendered
    assert 'evidence.json' in rendered
