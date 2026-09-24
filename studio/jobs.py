"""Bounded local worker queue and persistent, content-addressed analysis cache."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import logging
import threading

from studio.analysis import analyze, parse_capture, unique_observations

ANALYSIS_VERSION = 3


def selected_observations(data):
    return [o for o in data['observations'] if o['origin'] in data.get('selected', [])]


def report_key(data):
    # Hash content, including masking choices; restored workspaces cannot reuse stale reports.
    payload = {'engine': ANALYSIS_VERSION, 'observations': selected_observations(data), 'rules': data['rules']}
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def report_for(store, data):
    key = report_key(data)
    cached = store.cached_report(key)
    if cached is not None:
        return cached
    report = analyze(selected_observations(data), data['rules'])
    store.cache_report(key, report)
    return report


class Jobs:
    def __init__(self, store):
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='har-analysis')
        self.lock = threading.Lock()
        self.pending = {}
        store.interrupt_jobs()

    def submit(self, project_id, kind, task, draft_id=None):
        identifier = self.store.create_job(project_id, kind, draft_id)
        self.executor.submit(self._run, identifier, task)
        return identifier

    def _run(self, identifier, task):
        self.store.update_job(identifier, state='running')
        try:
            target, draft_id = task(identifier)
            self.store.update_job(identifier, state='done', progress=100, target=target, draft_id=draft_id)
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            self.store.update_job(identifier, state='failed', error=str(exc) or 'Не удалось обработать данные.')
        except Exception:
            logging.exception('Background job %s failed', identifier)
            self.store.update_job(identifier, state='failed', error='Ошибка обработки. Повторите действие или проверьте файл.')

    def import_files(self, project, files, data):
        def task(identifier):
            observations = list(data['observations'])
            for number, (filename, raw) in enumerate(files):
                self.store.update_job(identifier, filename=filename, progress=int(90 * number / len(files)))
                def progress(done, total):
                    self.store.update_job(identifier, progress=int(90 * (number + done / max(1, total)) / len(files)))
                try:
                    observations.extend(parse_capture(raw, filename, data['sensitive_names'], progress=progress))
                except (ValueError, TypeError, KeyError, RecursionError) as exc:
                    message = str(exc)
                    raise ValueError(message if message.startswith(filename + ':') else f'{filename}: {message}') from exc
            observations = unique_observations(observations)
            if not observations:
                raise ValueError('В выбранных HAR нет HTTP-запросов.')
            if len(observations) > 20000:
                raise ValueError('В одной версии допускается до 20 000 уникальных записей.')
            data['observations'] = observations
            self.store.update_job(identifier, progress=95, filename='')
            draft_id = self.store.create_draft(project['id'], data)
            return f'/drafts/{draft_id}/domains', draft_id
        return self.submit(project['id'], 'import', task)

    def analyze_draft(self, draft):
        def task(identifier):
            self.store.update_job(identifier, progress=15)
            report_for(self.store, draft['data'])
            self.store.update_job(identifier, progress=90)
            return f"/drafts/{draft['id']}", draft['id']
        return self.submit(draft['project_id'], 'analysis', task, draft['id'])

    def shutdown(self):
        self.executor.shutdown(wait=True)
