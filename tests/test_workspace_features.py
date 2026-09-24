from copy import deepcopy
import gzip
from io import BytesIO
import json
import re
import sqlite3
import threading
from unittest.mock import patch

import pytest
import yaml
from openapi_spec_validator import validate
from openapi_schema_validator import OAS30Validator

from studio.analysis import analyze, compare, parse_capture
from studio.backup import read_backup
from studio.export import reusable_schemas
from studio.jobs import report_for
from studio.store import Store
from studio.web import create_app
from test_studio import workspace, har, new_project, upload, select, save, post, ready_get, wait_job


def pair(client, token, project):
    versions = []
    for name, raw in [('Before', har(('/items', {'total': 10}))), ('After', har(('/items', {'total': '10'})))]:
        draft = upload(client, token, project, raw)
        select(client, token, draft)
        versions.append(save(client, token, draft, 1, name))
    return versions


def test_comparison_highlights_expected_review_and_sample_sizes(workspace):
    app, client, token = workspace
    project = new_project(client, token)
    before, after = pair(client, token, project)
    page = ready_get(client, f'/projects/{project}/compare', query_string={'before': before, 'after': after})
    assert page.status_code == 200
    assert 'sample-summary' in page.text and 'type-value' in page.text
    assert 'integer' in page.text and 'string' in page.text
    store = app.extensions['store']
    a, b = store.get('versions', before), store.get('versions', after)
    change = compare(report_for(store, a['data']), report_for(store, b['data']))[0]
    snapshot = deepcopy(b)
    response = post(client, token, f'/projects/{project}/compare/review', {
        'before': before, 'after': after, 'change_id': change['id'], 'expected': '1', 'note': 'Approved in release 2'})
    assert response.status_code == 302
    page = ready_get(client, response.location)
    assert 'Approved in release 2' in page.text and 'expected-change' in page.text
    assert store.get('versions', after) == snapshot
    # Reviews apply to an ordered pair; swapping versions is a different comparison.
    assert not store.reviews(project, json.dumps([after, before]))
    post(client, token, f'/projects/{project}/compare/review', {
        'before': before, 'after': after, 'change_id': change['id'], 'expected': '0'})
    assert not store.reviews(project, json.dumps([before, after]))


def test_small_sample_notice_is_scoped_to_operation():
    before = analyze(parse_capture(har(*[('/items', {'id': i, 'email': 'x'}) for i in range(20)]), 'a.har'), {})
    after = analyze(parse_capture(har(('/items', {'id': 1})), 'b.har'), {})
    change = compare(before, after)[0]
    assert change['smaller_sample'] is True
    assert change['values'][0]['count'] == 20 and change['values'][1]['count'] == 1
    assert change['values'][0]['types'] == 'string' and change['values'][1]['types'] is None


def test_project_rename_archive_restore_and_delete_draft(workspace):
    app, client, token = workspace
    project = new_project(client, token)
    draft = upload(client, token, project, har(('/items', {})))
    select(client, token, draft)
    version = save(client, token, draft, 1)
    discarded = upload(client, token, project, har(('/other', {})))
    post(client, token, f'/projects/{project}/rename', {'name': 'New name'})
    assert app.extensions['store'].project(project)['name'] == 'New name'
    assert post(client, token, f'/projects/{project}/rename', {'name': ''}).status_code == 400
    assert post(client, token, f'/drafts/{discarded}/delete', {'revision': 0}).status_code == 400
    assert post(client, token, f'/drafts/{discarded}/delete', {'revision': 0, 'confirm': 'yes'}).status_code == 302
    assert app.extensions['store'].get('versions', version)
    assert post(client, token, f'/projects/{project}/archive', {'archived': '1'}).status_code == 302
    assert not app.extensions['store'].projects()
    assert 'New name' in client.get('/?archived=1').text
    assert ready_get(client, f'/versions/{version}').status_code == 200
    denied = post(client, token, f'/projects/{project}/import', {'files': (BytesIO(har(('/items', {}))), 'a.har')})
    assert denied.status_code == 400
    post(client, token, f'/projects/{project}/archive', {'archived': '0'})
    assert len(app.extensions['store'].projects()) == 1


def test_backup_roundtrip_is_additive_and_preserves_links_reviews_and_archive(workspace, tmp_path):
    app, client, token = workspace
    project = new_project(client, token)
    before, after = pair(client, token, project)
    draft = upload(client, token, project, har(('/items', {})), mode='extend', base_id=after)
    store = app.extensions['store']
    changes = compare(report_for(store, store.get('versions', before)['data']), report_for(store, store.get('versions', after)['data']))
    store.review_change(project, json.dumps([before, after]), changes[0]['id'], True, 'Expected')
    store.archive_project(project, True)
    backup = client.get('/workspace/backup')
    assert backup.status_code == 200
    payload = read_backup(backup.data)
    assert set(payload) == {'format', 'version', 'projects', 'drafts', 'versions', 'reviews'}
    original = store.get('versions', after)
    response = post(client, token, '/workspace', {'backup': (BytesIO(backup.data), 'backup.json.gz'), 'confirm': 'yes'})
    assert response.status_code == 302
    restored = next(p for p in store.projects(True) if p['id'] != project)
    assert len(store.projects(True)) == 2
    assert store.get('versions', after) == original
    versions = store.versions(restored['id'])
    ids = {v['name']: v['id'] for v in versions}
    assert set(ids) == {'Before', 'After'}
    assert store.drafts(restored['id'])[0]['data']['base_id'] == ids['After']
    assert store.reviews(restored['id'], json.dumps([ids['Before'], ids['After']])) == {changes[0]['id']: 'Expected'}
    assert ready_get(client, f"/versions/{ids['After']}").status_code == 200
    # Invalid relationships reject the entire restore transaction.
    payload['drafts'][0]['project_id'] = 'f' * 32
    damaged = gzip.compress(json.dumps(payload).encode())
    response = post(client, token, '/workspace', {'backup': (BytesIO(damaged), 'bad.gz'), 'confirm': 'yes'})
    assert response.status_code == 400
    assert len(store.projects(True)) == 2
    assert post(client, token, '/workspace', {'backup': (BytesIO(b'garbage'), 'bad.gz'), 'confirm': 'yes'}).status_code == 400


def test_restore_requires_confirmation_and_enforces_expanded_size(workspace):
    _app, client, token = workspace
    assert post(client, token, '/workspace', {}).status_code == 400
    with patch('studio.backup.MAX_BACKUP_BYTES', 10):
        with pytest.raises(ValueError):
            read_backup(gzip.compress(b' ' * 11))


def test_background_import_returns_before_processing_finishes(workspace):
    app, client, token = workspace
    project = new_project(client, token)
    started, release = threading.Event(), threading.Event()
    original = parse_capture
    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    with patch('studio.jobs.parse_capture', blocked):
        try:
            response = client.post(f'/projects/{project}/import', data={'csrf': token, 'files': (BytesIO(har(('/items', {}))), 'one.har')})
            assert response.status_code == 302 and response.location.startswith('/jobs/')
            assert started.wait(2)
            job_id = response.location.split('/')[-1]
            status = client.get(f'/jobs/{job_id}/status').json
            assert status['state'] == 'running' and status['filename'] == 'one.har'
            assert client.get('/').status_code == 200
            assert client.get(response.location).status_code == 200
            assert not app.extensions['store'].drafts(project)
        finally:
            release.set()
        job = wait_job(client, response.location)
        assert job['state'] == 'done' and job['progress'] == 100
        assert app.extensions['store'].drafts(project)


def test_failed_batch_identifies_file_and_does_not_save_partial_draft(workspace):
    app, client, token = workspace
    project = new_project(client, token)
    response = client.post(f'/projects/{project}/import', data={'csrf': token, 'files': [
        (BytesIO(har(('/items', {}))), 'valid.har'), (BytesIO(b'{invalid'), 'broken.har')]})
    job = wait_job(client, response.location)
    assert job['state'] == 'failed' and job['filename'] == 'broken.har'
    assert 'broken.har' in job['error']
    assert not app.extensions['store'].drafts(project)


def test_report_cache_reused_on_reload_and_after_restart(workspace, tmp_path):
    app, client, token = workspace
    project = new_project(client, token)
    draft = upload(client, token, project, har(('/items', {'id': 1})))
    select(client, token, draft)
    version = save(client, token, draft, 1)
    with patch('studio.jobs.analyze', side_effect=AssertionError('Cache was not used')):
        assert ready_get(client, f'/versions/{version}').status_code == 200
        assert ready_get(client, f'/versions/{version}').status_code == 200
        restarted = create_app(tmp_path)
        assert ready_get(restarted.test_client(), f'/versions/{version}').status_code == 200
        restarted.extensions['jobs'].shutdown()


def test_legacy_database_migration_and_interrupted_jobs(tmp_path):
    path = tmp_path / 'workspace.sqlite3'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL, rules TEXT NOT NULL)')
    db.execute('INSERT INTO projects VALUES (?, ?, ?, ?)', ('a' * 32, 'Legacy', '2026-01-01', '{}'))
    db.commit()
    db.close()
    store = Store(path)
    assert store.project('a' * 32)['archived'] == 0
    identifier = store.create_job('a' * 32, 'import')
    app = create_app(tmp_path)
    assert app.extensions['store'].job(identifier)['state'] == 'failed'
    assert app.extensions['store'].project('a' * 32)['name'] == 'Legacy'
    app.extensions['jobs'].shutdown()


def test_reusable_components_are_valid_deterministic_and_do_not_modify_evidence():
    observations = parse_capture(har(('/users', {'id': 1, 'address': {'city': 'A'}}),
                                     ('/profile', {'id': 2, 'address': {'city': 'B'}})), 'a.har')
    original = next(iter(analyze(observations, {})['documents'].values()))
    snapshot = deepcopy(original)
    exported = reusable_schemas(original)
    validate(exported)
    assert original == snapshot
    assert exported == reusable_schemas(original)
    assert exported['components']['schemas']
    left = exported['paths']['/users']['get']['responses']['200']['content']['application/json']['schema']
    right = exported['paths']['/profile']['get']['responses']['200']['content']['application/json']['schema']
    assert left == right and '$ref' in left
    # Resolve refs against the document, then validate the captured example.
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT4
    registry = Registry().with_resource('urn:api', Resource.from_contents(exported, default_specification=DRAFT4))
    OAS30Validator({'$ref': 'urn:api' + left['$ref']}, registry=registry).validate({'id': 1, 'address': {'city': 'A'}})


def test_language_switch_all_screens_and_report_keep_user_content(workspace):
    app, client, token = workspace
    project = new_project(client, token)
    post(client, token, f'/projects/{project}/rename', {'name': 'Shop API'})
    before, after = pair(client, token, project)
    assert post(client, token, '/language', {'language': 'en', 'next': '/'}).status_code == 302
    for url in ('/', '/workspace', f'/projects/{project}', f'/versions/{before}',
                f'/projects/{project}/compare?before={before}&after={after}'):
        page = ready_get(client, url)
        assert page.status_code == 200
        assert '<html lang="en">' in page.text
        assert not re.search('[А-Яа-яЁё]', page.text), url
    error = post(client, token, f'/projects/{project}/rename', {'name': ''})
    assert 'Enter a project name' in error.text
    assert not re.search('[А-Яа-яЁё]', error.text)
    from zipfile import ZipFile
    with ZipFile(BytesIO(ready_get(client, f'/versions/{after}/export').data)) as archive:
        html = archive.read('report.html').decode()
        assert '<html lang="en">' in html and 'Shop API' in html
        assert not re.search('[А-Яа-яЁё]', html)
    post(client, token, '/language', {'language': 'ru', 'next': '/'})
    assert 'Ваши проекты' in client.get('/').text
    # Language switch cannot redirect to an external site.
    response = post(client, token, '/language', {'language': 'en', 'next': '//example.com'})
    assert response.location == '/'


def test_saved_comparison_report_includes_reviews_without_interactive_forms(workspace):
    from zipfile import ZipFile
    app, client, token = workspace
    project = new_project(client, token)
    draft = upload(client, token, project, har(('/items', {'total': 10})))
    select(client, token, draft)
    before = save(client, token, draft, 1, 'Baseline')
    draft = upload(client, token, project, har(('/items', {'total': '10'})), mode='compare', base_id=before)
    select(client, token, draft)
    after = save(client, token, draft, 1, 'Release')
    store = app.extensions['store']
    change = compare(report_for(store, store.get('versions', before)['data']), report_for(store, store.get('versions', after)['data']))[0]
    response = post(client, token, f'/projects/{project}/compare/review', {
        'before': before, 'after': after, 'change_id': change['id'], 'expected': '1',
        'note': '<b>Approved release</b>', 'return_to': 'version'})
    assert response.location.startswith(f'/versions/{after}')
    page = ready_get(client, response.location)
    assert 'sample-summary' in page.text
    assert '&lt;b&gt;Approved release&lt;/b&gt;' in page.text
    with ZipFile(BytesIO(ready_get(client, f'/versions/{after}/export').data)) as archive:
        html = archive.read('report.html').decode()
        assert '&lt;b&gt;Approved release&lt;/b&gt;' in html
        assert '<form' not in html and 'name="csrf"' not in html
