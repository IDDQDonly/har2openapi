"""Local-only Flask UI; uploaded captures become masked observations."""

import argparse
from collections import Counter
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import secrets
from zipfile import ZipFile, ZIP_DEFLATED

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for, jsonify
import yaml
from werkzeug.exceptions import SecurityError

from studio.analysis import compare, validate_template
from studio.store import Store, ConflictError
from studio.jobs import Jobs, report_for, report_key
from studio.backup import read_backup
from studio.export import reusable_schemas
from studio.i18n import translate as _, language as current_language, change_message, error_message


class AnalysisPending(Exception):
    def __init__(self, job_id):
        self.job_id = job_id


def create_app(data_dir=None):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=secrets.token_hex(32), MAX_CONTENT_LENGTH=50 * 1024 * 1024,
                      SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_HTTPONLY=True,
                      TRUSTED_HOSTS=['localhost', '127.0.0.1', '[::1]'])
    store = Store(Path(data_dir or '.har2openapi') / 'workspace.sqlite3')
    app.extensions['store'] = store
    jobs = Jobs(store)
    app.extensions['jobs'] = jobs

    @app.before_request
    def protect_forms():
        session.setdefault('csrf', secrets.token_hex(32))
        if request.method == 'POST' and not secrets.compare_digest(
                session['csrf'].encode(), request.form.get('csrf', '').encode()):
            abort(400, description='Страница устарела. Обновите её и повторите действие.')

    @app.after_request
    def headers(response):
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.context_processor
    def shared():
        return {'csrf': session.get('csrf', ''), '_': _, 'language': current_language(),
                'change_message': change_message, 'error_message': error_message}

    @app.errorhandler(KeyError)
    def missing(_error):
        return render_template('error.html', message='Проект, черновик или версия не найдены.'), 404

    @app.errorhandler(413)
    def too_large(_error):
        return render_template('error.html', message='Импорт ограничен 50 МБ. Разделите файлы на несколько загрузок.'), 413

    @app.errorhandler(400)
    def bad_request(error):
        if isinstance(error, SecurityError):
            return error.get_response()
        return render_template('error.html', message=error.description), 400

    @app.errorhandler(ValueError)
    def invalid(error):
        return render_template('error.html', message=str(error)), 409 if isinstance(error, ConflictError) else 400

    def get_record(kind, identifier):
        record = store.get(kind, identifier)
        return record, store.project(record['project_id'])

    def selected_observations(data):
        return [o for o in data['observations'] if o['origin'] in data.get('selected', [])]

    def result(data, project_id):
        key = report_key(data)
        cached = store.cached_report(key)
        if cached is not None:
            return cached
        if request.method == 'POST':
            return report_for(store, data)
        target = request.full_path.rstrip('?')
        with jobs.lock:
            if len(jobs.pending) > 100:
                jobs.pending = {k: v for k, v in jobs.pending.items() if store.job(v)['state'] in {'queued', 'running'}}
            pending = jobs.pending.get((key, target))
            if pending and store.job(pending)['state'] in {'queued', 'running'}:
                raise AnalysisPending(pending)
            def task(identifier):
                store.update_job(identifier, progress=20)
                report_for(store, data)
                return target, None
            identifier = jobs.submit(project_id, 'analysis', task)
            jobs.pending[(key, target)] = identifier
        raise AnalysisPending(identifier)

    @app.errorhandler(AnalysisPending)
    def waiting(error):
        return redirect(url_for('job_page', identifier=error.job_id))

    def comparison_context(data, report, project_id, record=None):
        if data['mode'] != 'compare' or not data.get('base_id'):
            return {'changes': None}
        baseline = store.get('versions', data['base_id'])
        observations = [o for o in selected_observations(baseline['data']) if o['origin'] in data['selected']]
        before = result({**baseline['data'], 'observations': observations, 'rules': data['rules']}, project_id)
        context = {'changes': compare(before, report), 'before_count': before['count'], 'after_count': report['count']}
        if record and 'name' in record:
            context.update(left=baseline, right=record,
                           reviews=store.reviews(project_id, json.dumps([baseline['id'], record['id']])))
        return context

    @app.get('/')
    def home():
        return render_template('home.html', projects=store.projects(request.args.get('archived') == '1'), archived=request.args.get('archived') == '1')

    @app.post('/projects')
    def create_project():
        identifier = store.create_project(request.form.get('name', ''))
        return redirect(url_for('project_page', identifier=identifier))

    @app.get('/projects/<identifier>')
    def project_page(identifier):
        project = store.project(identifier)
        return render_template('project.html', project=project, versions=store.versions(identifier),
                               drafts=store.drafts(identifier), rules=project['rules'], jobs=store.jobs(identifier))

    @app.post('/projects/<identifier>/import')
    def import_files(identifier):
        project = store.project(identifier)
        if project['archived']:
            raise ValueError('Верните проект из архива, чтобы добавить записи.')
        mode = request.form.get('mode', 'initial')
        if mode not in {'initial', 'extend', 'compare'}:
            raise ValueError('Выберите способ импорта.')
        base_id = request.form.get('base_id') or None
        observations = []
        if mode in {'extend', 'compare'}:
            if not base_id:
                raise ValueError('Выберите исходную версию.')
            baseline = store.get('versions', base_id)
            if baseline['project_id'] != identifier:
                abort(400, description='Исходная версия относится к другому проекту.')
            if mode == 'extend':
                observations = selected_observations(baseline['data'])
        else:
            base_id = None
        files = [f for f in request.files.getlist('files') if f.filename]
        if not files:
            raise ValueError('Выберите хотя бы один HAR-файл.')
        names = [v.strip() for v in request.form.get('sensitive_names', '').split(',') if v.strip()]
        uploads = [(f.filename.replace('\\', '/').split('/')[-1][:150], f.read()) for f in files]
        job_id = jobs.import_files(project, uploads, {
            'mode': mode, 'base_id': base_id, 'observations': observations,
            'selected': [], 'rules': deepcopy(project['rules']), 'sensitive_names': names})
        return redirect(url_for('job_page', identifier=job_id))

    @app.route('/drafts/<identifier>/domains', methods=['GET', 'POST'])
    def domains(identifier):
        draft, project = get_record('drafts', identifier)
        data = draft['data']
        counts = Counter(o['origin'] for o in data['observations'])
        if data['mode'] == 'compare' and data.get('base_id'):
            for o in selected_observations(store.get('versions', data['base_id'])['data']):
                counts.setdefault(o['origin'], 0)
        if request.method == 'POST':
            selected = request.form.getlist('domains')
            if not selected or not set(selected).issubset(counts):
                raise ValueError('Выберите хотя бы один из найденных доменов.')
            data['selected'] = sorted(set(selected))
            store.update_draft(identifier, data, int(request.form['revision']))
            job_id = jobs.analyze_draft(store.get('drafts', identifier))
            return redirect(url_for('job_page', identifier=job_id))
        return render_template('domains.html', project=project, draft=draft, counts=counts)

    @app.get('/drafts/<identifier>')
    def draft_page(identifier):
        draft, project = get_record('drafts', identifier)
        if not draft['data']['selected']:
            return redirect(url_for('domains', identifier=identifier))
        report = result(draft['data'], project['id'])
        return render_template('result.html', project=project, record=draft, report=report,
                               draft=True, standalone=False, **comparison_context(draft['data'], report, project['id']))

    @app.post('/drafts/<identifier>/rules')
    def decide_route(identifier):
        draft, _project = get_record('drafts', identifier)
        data = draft['data']
        key = request.form.get('key', '')
        report = result(data, draft['project_id'])
        if key not in {s['key'] for s in report['suggestions']}:
            raise ValueError('Предложение уже обработано. Обновите страницу.')
        choice = request.form.get('choice')
        if choice == 'keep':
            data['rules'][key] = None
        elif choice == 'accept':
            data['rules'][key] = validate_template(key, request.form.get('template', '').strip())
        else:
            raise ValueError('Выберите действие для маршрутов.')
        store.update_draft(identifier, data, int(request.form['revision']))
        flash('Решение применено. Оно будет сохранено вместе с версией.')
        return redirect(url_for('draft_page', identifier=identifier))

    @app.post('/drafts/<identifier>/rules/reset')
    def reset_rule(identifier):
        draft, _project = get_record('drafts', identifier)
        data = draft['data']
        key = request.form.get('key', '')
        if key not in data['rules']:
            raise ValueError('Решение не найдено.')
        del data['rules'][key]
        data.setdefault('removed_rules', []).append(key)
        store.update_draft(identifier, data, int(request.form['revision']))
        return redirect(url_for('draft_page', identifier=identifier))

    @app.post('/drafts/<identifier>/save')
    def save_version(identifier):
        version_id = store.save_version(identifier, request.form.get('name', ''), int(request.form['revision']))
        return redirect(url_for('version_page', identifier=version_id))

    @app.get('/versions/<identifier>')
    def version_page(identifier):
        version, project = get_record('versions', identifier)
        report = result(version['data'], project['id'])
        return render_template('result.html', project=project, record=version, report=report,
                               draft=False, standalone=False, **comparison_context(version['data'], report, project['id'], version))

    @app.get('/<kind>/<identifier>/evidence')
    def evidence(kind, identifier):
        if kind not in {'drafts', 'versions'}:
            abort(404)
        record, project = get_record(kind, identifier)
        report = result(record['data'], project['id'])
        operation = next((o for o in report['operations'] if o['id'] == request.args.get('operation')), None)
        if operation is None:
            abort(404)
        context, field = request.args.get('context', ''), request.args.get('field', '')
        sources = operation['sources']
        if field:
            statistic = next((f for f in operation['fields'] if f['context'] == context and f['name'] == field), None)
            if statistic is None:
                abort(404)
            identifiers = set(statistic['sources'])
            sources = [s for s in sources if s['id'] in identifiers]
        page = max(1, request.args.get('page', 1, type=int))
        pages = max(1, (len(sources) + 24) // 25)
        page = min(page, pages)
        return render_template('evidence.html', project=project, record=record, operation=operation,
                               kind=kind, context=context, field=field, total=len(sources),
                               sources=sources[(page - 1) * 25:page * 25], page=page, pages=pages)

    @app.get('/projects/<identifier>/compare')
    def compare_versions(identifier):
        project = store.project(identifier)
        versions = store.versions(identifier)
        left_id, right_id = request.args.get('before'), request.args.get('after')
        changes = None
        left = right = None
        if left_id and right_id:
            left, right = store.get('versions', left_id), store.get('versions', right_id)
            if left['project_id'] != identifier or right['project_id'] != identifier:
                abort(400, description='Обе версии должны относиться к этому проекту.')
            rules = right['data']['rules']
            before_report = result({**left['data'], 'rules': rules}, identifier)
            after_report = result(right['data'], identifier)
            changes = compare(before_report, after_report)
        return render_template('compare.html', project=project, versions=versions, changes=changes,
                               left=left, right=right,
                               before_count=before_report['count'] if left else 0,
                               after_count=after_report['count'] if right else 0,
                               reviews=store.reviews(identifier, json.dumps([left_id, right_id])) if left else {})

    @app.get('/<kind>/<identifier>/export')
    def export(kind, identifier):
        if kind not in {'drafts', 'versions'}:
            abort(404)
        record, project = get_record(kind, identifier)
        report = result(record['data'], project['id'])
        if not record['data']['selected']:
            raise ValueError('Сначала выберите домены и выполните анализ.')
        output = BytesIO()
        css = (Path(app.static_folder) / 'style.css').read_text()
        html = render_template('report.html', project=project, record=record, report=report,
                               css=css, **comparison_context(record['data'], report, project['id'], record))
        with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
            for index, (origin, document) in enumerate(report['documents'].items(), 1):
                document = reusable_schemas(document)
                document['info']['version'] = record.get('name', 'draft')
                archive.writestr(f'openapi-{index}.yaml', yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
            archive.writestr('report.html', html)
            archive.writestr('evidence.json', json.dumps(report, ensure_ascii=False, indent=2))
        output.seek(0)
        return send_file(output, mimetype='application/zip', as_attachment=True,
                         download_name=f'har2openapi-{identifier[:8]}.zip')

    @app.get('/jobs/<identifier>')
    def job_page(identifier):
        job = store.job(identifier)
        if job['state'] == 'done':
            return redirect(job['target'])
        return render_template('job.html', job=job, project=store.project(job['project_id']))

    @app.get('/jobs/<identifier>/status')
    def job_status(identifier):
        job = store.job(identifier)
        job['label'] = _(job['state'])
        job['error'] = error_message(job['error'])
        job['open_label'] = _('Открыть результат')
        return jsonify(job)

    @app.post('/language')
    def language():
        choice = request.form.get('language')
        if choice not in {'en', 'ru'}:
            abort(400)
        session['language'] = choice
        target = request.form.get('next', '/')
        if not target.startswith('/') or target.startswith('//') or '\\' in target or '\n' in target:
            target = '/'
        return redirect(target)

    @app.post('/projects/<identifier>/rename')
    def rename_project(identifier):
        store.rename_project(identifier, request.form.get('name', ''))
        return redirect(url_for('project_page', identifier=identifier))

    @app.post('/projects/<identifier>/archive')
    def archive_project(identifier):
        store.archive_project(identifier, request.form.get('archived') == '1')
        return redirect(url_for('project_page', identifier=identifier))

    @app.post('/drafts/<identifier>/delete')
    def delete_draft(identifier):
        draft, project = get_record('drafts', identifier)
        if request.form.get('confirm') != 'yes':
            raise ValueError('Подтвердите удаление черновика.')
        store.delete_draft(identifier, int(request.form['revision']))
        return redirect(url_for('project_page', identifier=project['id']))

    @app.route('/workspace', methods=['GET', 'POST'])
    def workspace():
        if request.method == 'POST':
            if request.form.get('confirm') != 'yes':
                raise ValueError('Подтвердите восстановление копии как новых проектов.')
            uploaded = request.files.get('backup')
            if not uploaded or not uploaded.filename:
                raise ValueError('Выберите резервную копию.')
            payload = read_backup(uploaded.read())
            count = store.restore(payload)
            flash(_('Восстановлено проектов: {count}.', count=count))
            return redirect(url_for('home'))
        return render_template('workspace.html')

    @app.get('/workspace/backup')
    def backup():
        return send_file(BytesIO(store.backup()), mimetype='application/gzip', as_attachment=True,
                         download_name='har-studio-backup.json.gz')

    @app.post('/projects/<identifier>/compare/review')
    def review_change(identifier):
        left_id, right_id = request.form.get('before'), request.form.get('after')
        left, right = store.get('versions', left_id), store.get('versions', right_id)
        if left['project_id'] != identifier or right['project_id'] != identifier:
            abort(400)
        before = report_for(store, {**left['data'], 'rules': right['data']['rules']})
        after = report_for(store, right['data'])
        changes = compare(before, after)
        change_id = request.form.get('change_id')
        if change_id not in {c['id'] for c in changes}:
            raise ValueError('Изменение не найдено. Обновите сравнение.')
        store.review_change(identifier, json.dumps([left_id, right_id]), change_id,
                            request.form.get('expected') == '1', request.form.get('note', ''))
        target = (url_for('version_page', identifier=right_id) if request.form.get('return_to') == 'version'
                  else url_for('compare_versions', identifier=identifier, before=left_id, after=right_id))
        return redirect(target + '#change-' + change_id)

    return app


def main():
    parser = argparse.ArgumentParser(description='Local HAR to OpenAPI workspace')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', default='.har2openapi')
    args = parser.parse_args()
    create_app(args.data_dir).run(host='127.0.0.1', port=args.port, debug=False)


if __name__ == '__main__':
    main()
