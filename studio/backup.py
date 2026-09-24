"""Bounded, data-only workspace backups with strict relationship validation."""

import gzip
from io import BytesIO
import json
import re

from studio.analysis import validate_template

MAX_BACKUP_BYTES = 200 * 1024 * 1024


def read_backup(raw):
    try:
        with gzip.GzipFile(fileobj=BytesIO(raw)) as archive:
            decoded = archive.read(MAX_BACKUP_BYTES + 1)
        if len(decoded) > MAX_BACKUP_BYTES:
            raise ValueError
        payload = json.loads(decoded)
        validate_backup(payload)
        return payload
    except (OSError, EOFError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError('Некорректная резервная копия или превышен лимит 200 МБ после распаковки.') from exc


def validate_backup(payload):
    def require(condition):
        if not condition:
            raise ValueError('Некорректная структура резервной копии.')

    def text(value, maximum=1000):
        return isinstance(value, str) and len(value) <= maximum

    def rules(value):
        require(isinstance(value, dict))
        for key, template in value.items():
            parsed = json.loads(key)
            require(isinstance(parsed, list) and len(parsed) == 3 and all(text(x) for x in parsed))
            require(template is None or text(template))
            if template is not None:
                validate_template(key, template)

    require(isinstance(payload, dict) and payload.get('format') == 'har-studio-workspace' and payload.get('version') == 1)
    all_ids = set()
    for table in ('projects', 'versions', 'drafts', 'reviews'):
        require(isinstance(payload.get(table), list) and len(payload[table]) <= 10000)
        if table == 'reviews':
            continue
        for row in payload[table]:
            require(isinstance(row, dict) and isinstance(row.get('id'), str) and re.fullmatch('[0-9a-f]{32}', row['id']) is not None)
            require(row['id'] not in all_ids)
            all_ids.add(row['id'])
    projects = {row['id']: row for row in payload['projects']}
    versions = {row['id']: row for row in payload['versions']}
    for row in projects.values():
        require(text(row.get('name'), 100) and bool(row['name'].strip()) and text(row.get('created')))
        require(type(row.get('archived', 0)) is int and row.get('archived', 0) in (0, 1))
        rules(json.loads(row['rules']))
    for table in ('versions', 'drafts'):
        for row in payload[table]:
            require(row.get('project_id') in projects)
            if table == 'versions':
                require(text(row.get('name'), 100) and bool(row['name'].strip()) and text(row.get('created')))
            else:
                require(type(row.get('revision')) is int and row['revision'] >= 0)
            data = json.loads(row['data'])
            require(isinstance(data, dict) and data.get('mode') in ('initial', 'extend', 'compare'))
            require(isinstance(data.get('selected'), list) and all(text(x) for x in data['selected']))
            if data.get('base_id'):
                require(data['base_id'] in versions and versions[data['base_id']]['project_id'] == row['project_id'])
            rules(data.get('rules'))
            require(isinstance(data.get('removed_rules', []), list) and all(text(x) for x in data.get('removed_rules', [])))
            require(isinstance(data.get('sensitive_names', []), list) and all(text(x) for x in data.get('sensitive_names', [])))
            require(isinstance(data.get('observations'), list) and len(data['observations']) <= 20000)
            for observation in data['observations']:
                require(isinstance(observation, dict))
                for field in ('id', 'source', 'origin', 'path', 'method'):
                    require(text(observation.get(field), 10000))
                require(observation['method'] in ('get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace'))
                require(type(observation.get('index')) is int and observation['index'] > 0)
                require(isinstance(observation.get('contexts'), list) and all(text(x) for x in observation['contexts']))
                require(isinstance(observation.get('fields'), dict))
                for context, fields in observation['fields'].items():
                    require(text(context) and isinstance(fields, dict))
                    for name, types in fields.items():
                        require(text(name, 10000) and isinstance(types, list) and all(text(x) for x in types))
                operation = observation.get('operation')
                require(isinstance(operation, dict) and isinstance(operation.get('parameters'), list)
                        and isinstance(operation.get('responses'), dict))
                for parameter in operation['parameters']:
                    require(isinstance(parameter, dict) and text(parameter.get('name')) and text(parameter.get('in'))
                            and isinstance(parameter.get('schema'), dict))
                bodies = [operation['requestBody']] if 'requestBody' in operation else []
                require(all(text(key) for key in operation['responses']))
                bodies.extend(operation['responses'].values())
                for body in bodies:
                    require(isinstance(body, dict) and isinstance(body.get('content', {}), dict))
                    for mime, media in body.get('content', {}).items():
                        require(text(mime) and isinstance(media, dict) and isinstance(media.get('schema'), dict))
    for row in payload['reviews']:
        require(isinstance(row, dict) and row.get('project_id') in projects and text(row.get('note'), 500)
                and text(row.get('change_id'), 64))
        pair = json.loads(row['pair'])
        require(isinstance(pair, list) and len(pair) == 2 and all(
            item in versions and versions[item]['project_id'] == row['project_id'] for item in pair))
