"""SQLite persistence with atomic snapshots and optimistic draft revisions."""

from contextlib import contextmanager
import gzip
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


class ConflictError(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] > 2:
                raise ValueError('This workspace was created by a newer HAR Studio version.')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL, rules TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL, created TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS report_cache (
                    key TEXT PRIMARY KEY, report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    kind TEXT NOT NULL, state TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    filename TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    draft_id TEXT, target TEXT NOT NULL DEFAULT '', created TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    project_id TEXT NOT NULL REFERENCES projects(id), pair TEXT NOT NULL,
                    change_id TEXT NOT NULL, note TEXT NOT NULL,
                    PRIMARY KEY (project_id, pair, change_id)
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(projects)')}
            if 'archived' not in columns:
                db.execute('ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
            db.execute('PRAGMA user_version = 2')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys = ON')
            with db:
                yield db
        finally:
            db.close()

    def projects(self, archived=False):
        with self.connection() as db:
            return [dict(row) for row in db.execute('''
                SELECT p.*, (SELECT count(*) FROM versions v WHERE v.project_id=p.id) AS version_count
                FROM projects p WHERE p.archived=? ORDER BY p.created DESC
            ''', (int(archived),))]

    def create_project(self, name):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError('Введите название проекта от 1 до 100 символов.')
        identifier = uuid4().hex
        with self.connection() as db:
            db.execute('INSERT INTO projects (id, name, created, rules) VALUES (?, ?, ?, ?)',
                       (identifier, name, datetime.now(timezone.utc).isoformat(), '{}'))
        return identifier

    def project(self, identifier):
        with self.connection() as db:
            row = db.execute('SELECT * FROM projects WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        project = dict(row)
        project['rules'] = json.loads(project['rules'])
        return project

    def versions(self, project_id):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT id, name, created FROM versions WHERE project_id=? ORDER BY created DESC', (project_id,))]

    def drafts(self, project_id):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM drafts WHERE project_id=? ORDER BY rowid DESC', (project_id,))
            return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row):
        if row is None:
            raise KeyError('Not found')
        record = dict(row)
        record['data'] = json.loads(record['data'])
        return record

    def get(self, kind, identifier):
        if kind not in {'drafts', 'versions'}:
            raise ValueError('Invalid record type')
        with self.connection() as db:
            return self._decode(db.execute(f'SELECT * FROM {kind} WHERE id=?', (identifier,)).fetchone())

    def create_draft(self, project_id, data):
        identifier = uuid4().hex
        with self.connection() as db:
            db.execute('INSERT INTO drafts (id, project_id, data) VALUES (?, ?, ?)',
                       (identifier, project_id, json.dumps(data, ensure_ascii=False)))
        return identifier

    def update_draft(self, identifier, data, revision):
        with self.connection() as db:
            cursor = db.execute('UPDATE drafts SET data=?, revision=revision+1 WHERE id=? AND revision=?',
                                (json.dumps(data, ensure_ascii=False), identifier, revision))
            if not cursor.rowcount:
                raise ConflictError('Черновик изменился в другой вкладке. Обновите страницу.')

    def save_version(self, identifier, name, revision):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError('Введите название версии от 1 до 100 символов.')
        version_id = uuid4().hex
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            draft = self._decode(db.execute('SELECT * FROM drafts WHERE id=?', (identifier,)).fetchone())
            if draft['revision'] != revision:
                raise ConflictError('Черновик изменился. Обновите страницу перед сохранением.')
            data = draft['data']
            if not data.get('selected'):
                raise ValueError('Сначала выберите домены и проанализируйте записи.')
            data['observations'] = [o for o in data['observations'] if o['origin'] in data['selected']]
            db.execute('INSERT INTO versions VALUES (?, ?, ?, ?, ?)',
                       (version_id, draft['project_id'], name, datetime.now(timezone.utc).isoformat(),
                        json.dumps(data, ensure_ascii=False)))
            project_rules = json.loads(db.execute('SELECT rules FROM projects WHERE id=?',
                                                 (draft['project_id'],)).fetchone()['rules'])
            for key in data.get('removed_rules', []):
                project_rules.pop(key, None)
            project_rules.update(data['rules'])
            db.execute('UPDATE projects SET rules=? WHERE id=?', (json.dumps(project_rules), draft['project_id']))
            db.execute('DELETE FROM drafts WHERE id=?', (identifier,))
        return version_id

    def rename_project(self, identifier, name):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError('Введите название проекта от 1 до 100 символов.')
        with self.connection() as db:
            if not db.execute('UPDATE projects SET name=? WHERE id=?', (name, identifier)).rowcount:
                raise KeyError(identifier)

    def archive_project(self, identifier, archived):
        with self.connection() as db:
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running')", (identifier,)).fetchone():
                raise ConflictError('Дождитесь завершения обработки перед архивированием.')
            if not db.execute('UPDATE projects SET archived=? WHERE id=?', (int(archived), identifier)).rowcount:
                raise KeyError(identifier)

    def delete_draft(self, identifier, revision):
        with self.connection() as db:
            if db.execute("SELECT 1 FROM jobs WHERE draft_id=? AND state IN ('queued','running')", (identifier,)).fetchone():
                raise ConflictError('Дождитесь завершения обработки перед удалением черновика.')
            if not db.execute('DELETE FROM drafts WHERE id=? AND revision=?', (identifier, revision)).rowcount:
                raise ConflictError('Черновик изменился. Обновите страницу.')

    def cached_report(self, key):
        with self.connection() as db:
            row = db.execute('SELECT report FROM report_cache WHERE key=?', (key,)).fetchone()
        return json.loads(row['report']) if row else None

    def cache_report(self, key, report):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO report_cache VALUES (?, ?)', (key, json.dumps(report, ensure_ascii=False)))
            # Derived data can be safely evicted; snapshots remain untouched.
            db.execute('DELETE FROM report_cache WHERE rowid NOT IN (SELECT rowid FROM report_cache ORDER BY rowid DESC LIMIT 100)')

    def create_job(self, project_id, kind, draft_id=None):
        identifier = uuid4().hex
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 4:
                raise ConflictError('Очередь заполнена. Дождитесь завершения текущих задач.')
            db.execute('INSERT INTO jobs (id, project_id, kind, state, draft_id, created) VALUES (?, ?, ?, ?, ?, ?)',
                       (identifier, project_id, kind, 'queued', draft_id, datetime.now(timezone.utc).isoformat()))
        return identifier

    def update_job(self, identifier, **values):
        if not values or not set(values) <= {'state', 'progress', 'filename', 'error', 'draft_id', 'target'}:
            raise ValueError('Invalid job update')
        with self.connection() as db:
            db.execute('UPDATE jobs SET ' + ', '.join(f'{key}=?' for key in values) + ' WHERE id=?',
                       (*values.values(), identifier))

    def job(self, identifier):
        with self.connection() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return dict(row)

    def jobs(self, project_id):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 20', (project_id,))]

    def interrupt_jobs(self):
        with self.connection() as db:
            db.execute("UPDATE jobs SET state='failed', error=? WHERE state IN ('queued','running')",
                       ('Обработка прервана перезапуском. Повторите действие; сохранённые версии не изменились.',))

    def reviews(self, project_id, pair):
        with self.connection() as db:
            return {row['change_id']: row['note'] for row in db.execute(
                'SELECT change_id, note FROM reviews WHERE project_id=? AND pair=?', (project_id, pair))}

    def review_change(self, project_id, pair, change_id, expected, note=''):
        if len(note) > 500:
            raise ValueError('Комментарий должен быть не длиннее 500 символов.')
        with self.connection() as db:
            if expected:
                db.execute('INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?)', (project_id, pair, change_id, note.strip()))
            else:
                db.execute('DELETE FROM reviews WHERE project_id=? AND pair=? AND change_id=?', (project_id, pair, change_id))

    def backup(self):
        """One consistent read transaction; portable data only, no SQL or derived cache."""
        with self.connection() as db:
            db.execute('BEGIN')
            payload = {'format': 'har-studio-workspace', 'version': 1}
            for table in ('projects', 'versions', 'drafts', 'reviews'):
                payload[table] = [dict(row) for row in db.execute(f'SELECT * FROM {table}')]
        return gzip.compress(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

    def restore(self, payload):
        """Restore validated data as new projects. Never replace existing records."""
        from studio.backup import validate_backup
        validate_backup(payload)
        ids = {row['id']: uuid4().hex for table in ('projects', 'versions', 'drafts') for row in payload[table]}
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in payload['projects']:
                db.execute('INSERT INTO projects (id, name, created, rules, archived) VALUES (?, ?, ?, ?, ?)',
                           (ids[row['id']], row['name'], row['created'], row['rules'], row.get('archived', 0)))
            for table in ('versions', 'drafts'):
                for row in payload[table]:
                    data = json.loads(row['data'])
                    if data.get('base_id'):
                        data['base_id'] = ids[data['base_id']]
                    if table == 'versions':
                        db.execute('INSERT INTO versions VALUES (?, ?, ?, ?, ?)',
                                   (ids[row['id']], ids[row['project_id']], row['name'], row['created'], json.dumps(data, ensure_ascii=False)))
                    else:
                        db.execute('INSERT INTO drafts VALUES (?, ?, ?, ?)',
                                   (ids[row['id']], ids[row['project_id']], json.dumps(data, ensure_ascii=False), row['revision']))
            for row in payload.get('reviews', []):
                pair = json.dumps([ids[item] for item in json.loads(row['pair'])])
                db.execute('INSERT INTO reviews VALUES (?, ?, ?, ?)',
                           (ids[row['project_id']], pair, row['change_id'], row['note']))
        return len(payload['projects'])
