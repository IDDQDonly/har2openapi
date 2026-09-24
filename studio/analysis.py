"""Evidence-based analysis independent of the web interface and persistence."""

from collections import Counter, defaultdict
from copy import deepcopy
from hashlib import sha256
import json
import re

from har2openapi import har2openapi


ID_SEGMENT = re.compile(r'(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\Z')
PARAMETER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}\Z')


def merge_schema(left, right):
    """Merge observations structurally; never infer required properties."""
    if left == right:
        return deepcopy(left)
    if not left or not right:
        return deepcopy(left or right)
    if left.get('type') == right.get('type') and 'type' in left:
        result = deepcopy(left)
        if right.get('nullable'):
            result['nullable'] = True
        if left['type'] == 'object':
            properties = result.setdefault('properties', {})
            for name, schema in right.get('properties', {}).items():
                properties[name] = merge_schema(properties.get(name, {}), schema)
        elif left['type'] == 'array':
            result['items'] = merge_schema(left.get('items', {}), right.get('items', {}))
        return result
    variants = []
    for schema in left.get('anyOf', [left]) + right.get('anyOf', [right]):
        for i, existing in enumerate(variants):
            if schema.get('type') and schema.get('type') == existing.get('type'):
                variants[i] = merge_schema(existing, schema)
                break
        else:
            if schema not in variants:
                variants.append(deepcopy(schema))
    return variants[0] if len(variants) == 1 else {'anyOf': variants}


def walk_fields(value, prefix='$', result=None):
    """Collect types per body observation, counting an array field once per response."""
    result = {} if result is None else result
    kind = ('null' if value is None else 'boolean' if isinstance(value, bool) else
            'integer' if isinstance(value, int) else 'number' if isinstance(value, float) else
            'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else 'string')
    result.setdefault(prefix, set()).add(kind)
    if isinstance(value, dict):
        for name, item in value.items():
            escaped = name.replace('~', '~0').replace('/', '~1')
            walk_fields(item, f'{prefix}/{escaped}', result)
    elif isinstance(value, list):
        for item in value:
            walk_fields(item, f'{prefix}/*', result)
    return result


def parse_capture(raw, filename, sensitive_names=None, progress=None):
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f'{filename}: файл не содержит корректный JSON.') from exc
    converter = har2openapi('', sensitive_names=sensitive_names)
    try:
        converter.load_data(data)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f'{filename}: некорректная структура HAR. {exc}') from exc
    if len(converter.entries) > 10000:
        raise ValueError('В одном HAR допускается не более 10 000 записей.')
    digest = sha256(raw).hexdigest()
    observations = []
    total = len(converter.entries)
    for index, entry in enumerate(converter.entries, 1):
        if progress and (index == 1 or index % 100 == 0 or index == total):
            progress(index, total)
        converter.entries = [entry]
        documents = converter.build_documents()
        origin, document = next(iter(documents.items()))
        path, methods = next(iter(document['paths'].items()))
        method, operation = next(iter(methods.items()))
        fields, contexts = {}, []
        bodies = [('request', operation.get('requestBody', {}))]
        bodies += [(f'response {status}', response) for status, response in operation['responses'].items()]
        for label, body in bodies:
            for mime, media in body.get('content', {}).items():
                if 'example' not in media:
                    continue
                context = f'{label} · {mime}'
                contexts.append(context)
                # Count types before masking, but persist no original values here.
                raw_body = (entry['request'].get('postData', {}) if label == 'request'
                            else entry['response'].get('content', {})).get('text', '')
                converter.mask_secrets = False
                original_media = converter._media(mime, raw_body)
                converter.mask_secrets = True
                fields[context] = {name: sorted(types) for name, types in walk_fields(
                    original_media[mime]['example']).items()}
        observations.append({'id': f'{digest}:{index}', 'source': filename, 'index': index,
                             'origin': origin, 'path': path, 'method': method,
                             'operation': operation, 'fields': fields, 'contexts': contexts})
    return observations


def unique_observations(observations):
    return list({o['id']: o for o in observations}.values())


def route_pattern(path):
    parts = path.split('/')
    dynamic = []
    for index, segment in enumerate(parts):
        if ID_SEGMENT.fullmatch(segment):
            parts[index] = '{}'
            dynamic.append(index)
    return '/'.join(parts), dynamic


def route_key(observation):
    pattern, _ = route_pattern(observation['path'])
    return json.dumps([observation['origin'], observation['method'], pattern])


def suggest_routes(observations, rules):
    groups = defaultdict(list)
    for observation in observations:
        pattern, positions = route_pattern(observation['path'])
        if positions:
            groups[route_key(observation)].append(observation)
    suggestions = []
    for key, group in sorted(groups.items()):
        paths = sorted({o['path'] for o in group})
        if key in rules or len(paths) < 2:
            continue
        pattern, positions = route_pattern(paths[0])
        parts = pattern.split('/')
        used = set()
        for i in positions:
            parent = re.sub('[^a-zA-Z0-9_]', '_', parts[i - 1]).rstrip('s') or 'item'
            name = f'{parent}_id'
            if not re.match('[a-zA-Z_]', name):
                name = f'item_{name}'
            if name in used:
                name += f'_{i}'
            used.add(name)
            parts[i] = '{' + name + '}'
        suggestions.append({'key': key, 'origin': group[0]['origin'], 'method': group[0]['method'],
                            'paths': paths, 'template': '/'.join(parts), 'count': len(group)})
    return suggestions


def validate_template(key, template):
    pattern = json.loads(key)[2].split('/')
    parts = template.split('/')
    if len(pattern) != len(parts):
        raise ValueError('Сохраните число сегментов маршрута; изменять можно только имена параметров.')
    names = []
    for expected, actual in zip(pattern, parts):
        if expected != '{}':
            if expected != actual:
                raise ValueError('Статические части маршрута должны остаться неизменными.')
        else:
            match = PARAMETER.fullmatch(actual)
            if not match:
                raise ValueError('Имя параметра: {user_id}, латинские буквы, цифры и подчёркивание.')
            names.append(match[1])
    if len(names) != len(set(names)):
        raise ValueError('Имена параметров внутри маршрута должны быть разными.')
    return template


def merge_content(target, incoming):
    for mime, media in incoming.items():
        if mime not in target:
            target[mime] = deepcopy(media)
        else:
            target[mime]['schema'] = merge_schema(target[mime]['schema'], media['schema'])
            if 'example' in media:
                target[mime]['example'] = deepcopy(media['example'])


def field_groups(fields):
    """Readable overview; retain raw evidence separately for exports and comparison."""
    grouped = defaultdict(list)
    for field in fields:
        grouped[field['context']].append(field)
    result = []
    for context, entries in grouped.items():
        parents = {field['name'].rsplit('/', 1)[0] for field in entries if '/' in field['name']}
        visible = []
        for field in entries:
            # Keep empty containers and mixed scalar/container types visible.
            if field['name'] in parents and set(field['types']) <= {'object', 'array'}:
                continue
            label = ''
            for part in field['name'].split('/')[1:]:
                part = part.replace('~1', '/').replace('~0', '~')
                if part == '*':
                    label += '[]'
                elif re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', part):
                    label += ('.' if label else '') + part
                else:
                    label += '[' + json.dumps(part, ensure_ascii=False) + ']'
            visible.append({**field, 'label': label, 'in_array': '*' in field['name'].split('/')})
        direction, _, mime = context.partition(' · ')
        result.append({'request': direction == 'request', 'status': direction.removeprefix('response '),
                       'mime': mime, 'total': entries[0]['total'], 'fields': visible,
                       'has_arrays': any(field['in_array'] for field in visible)})
    return result


def analyze(observations, rules):
    groups = defaultdict(list)
    for observation in unique_observations(observations):
        path = rules.get(route_key(observation)) or observation['path']
        groups[(observation['origin'], path, observation['method'])].append(observation)
    operations, documents = [], {}
    for (origin, path, method), group in sorted(groups.items()):
        operation = {'summary': f'{method.upper()} {path}', 'parameters': [], 'responses': {}}
        parameters = {}
        contexts = Counter()
        field_stats = {}
        for observation in group:
            observed = observation['operation']
            for parameter in observed['parameters']:
                key = (parameter['in'], parameter['name'])
                if key not in parameters:
                    parameters[key] = deepcopy(parameter)
                else:
                    parameters[key]['schema'] = merge_schema(parameters[key]['schema'], parameter['schema'])
            if 'requestBody' in observed:
                body = operation.setdefault('requestBody', {'required': False, 'content': {}})
                merge_content(body['content'], observed['requestBody']['content'])
            for status, response in observed['responses'].items():
                target = operation['responses'].setdefault(status, {'description': response['description']})
                if 'content' in response:
                    merge_content(target.setdefault('content', {}), response['content'])
            contexts.update(observation['contexts'])
            for context, fields in observation['fields'].items():
                for field, types in fields.items():
                    stat = field_stats.setdefault((context, field), {'count': 0, 'types': Counter(), 'sources': []})
                    stat['count'] += 1
                    stat['types'].update(types)
                    stat['sources'].append(observation['id'])
        operation['parameters'] = list(parameters.values())
        for segment in path.split('/'):
            match = PARAMETER.fullmatch(segment)
            if match:
                operation['parameters'].append({'name': match[1], 'in': 'path', 'required': True,
                                                'schema': {'type': 'string'}})
        fields = [{'context': context, 'name': field, 'total': contexts[context], **stat,
                   'types': dict(stat['types'])} for (context, field), stat in sorted(field_stats.items())]
        sources = [{'id': o['id'], 'file': o['source'], 'index': o['index'], 'path': o['path'],
                    'example': o['operation']} for o in group]
        identifier = sha256(json.dumps([origin, path, method]).encode()).hexdigest()[:16]
        operations.append({'id': identifier, 'origin': origin, 'path': path, 'method': method,
                           'count': len(group), 'statuses': sorted(operation['responses']),
                           'fields': fields, 'field_groups': field_groups(fields),
                           'sources': sources, 'schema': operation})
        document = documents.setdefault(origin, {'openapi': '3.0.3', 'info': {
            'title': f'Observed API: {origin}', 'version': '1.0.0'}, 'servers': [{'url': origin}], 'paths': {}})
        document['paths'].setdefault(path, {})[method] = operation
    return {'operations': operations, 'documents': documents, 'count': len(unique_observations(observations)),
            'files': len({o['id'].split(':')[0] for o in observations}),
            'suggestions': suggest_routes(observations, rules)}


def compare(before, after):
    """Compare observed evidence, never interpret absence as proof of deletion."""
    old = {(o['origin'], o['path'], o['method']): o for o in before['operations']}
    new = {(o['origin'], o['path'], o['method']): o for o in after['operations']}
    changes = []
    def add(key, kind, message, old_op=None, new_op=None, context=None, field=None, status=None):
        def evidence(operation):
            if operation is None:
                return []
            sources = operation['sources']
            if field:
                stat = next((f for f in operation['fields'] if f['context'] == context and f['name'] == field), None)
                ids = set(stat['sources']) if stat else set()
                sources = [s for s in sources if s['id'] in ids]
            elif status:
                sources = [s for s in sources if status in s['example']['responses']]
            return sources
        changes.append({'origin': key[0], 'path': key[1], 'method': key[2], 'kind': kind,
                        'message': message, 'before': old_op, 'after': new_op,
                        'context': context, 'field': field,
                        'before_sources': evidence(old_op), 'after_sources': evidence(new_op),
                        'id': sha256(json.dumps([key, kind, message]).encode()).hexdigest()[:24]})
    for key in sorted(old.keys() | new.keys()):
        left, right = old.get(key), new.get(key)
        if left is None:
            add(key, 'new', 'Операция впервые встретилась в новой выборке.', None, right)
            continue
        if right is None:
            add(key, 'unobserved', 'Операция не встретилась в новых записях. Удаление не подтверждено.', left)
            continue
        for status in sorted(set(right['statuses']) - set(left['statuses'])):
            add(key, 'new', f'Впервые наблюдался ответ {status}.', left, right, status=status)
        for status in sorted(set(left['statuses']) - set(right['statuses'])):
            add(key, 'unobserved', f'Ответ {status} не встретился в новой выборке.', left, right, status=status)
        lf = {(f['context'], f['name']): f for f in left['fields']}
        rf = {(f['context'], f['name']): f for f in right['fields']}
        left_contexts = {context for context, _field in lf}
        right_contexts = {context for context, _field in rf}
        for context in sorted(left_contexts ^ right_contexts):
            direction = context.split(' · ', 1)[0]
            if direction.startswith('response '):
                status = direction.split(' ', 1)[1]
                if status not in left['statuses'] or status not in right['statuses']:
                    continue  # The new/unobserved status already explains this entire body.
            if context in right_contexts:
                add(key, 'new', f'{context}: впервые наблюдалось тело этого типа.', left, right, context, '$')
            else:
                add(key, 'unobserved', f'{context}: тело этого типа не встретилось в новой выборке.', left, right, context, '$')
        for context, field in sorted(lf.keys() | rf.keys()):
            if context not in left_contexts & right_contexts:
                continue
            a, b = lf.get((context, field)), rf.get((context, field))
            label = f'{context} → {field}'
            if a is None:
                add(key, 'new', f'{label}: поле впервые наблюдалось.', left, right, context, field)
            elif b is None:
                add(key, 'unobserved', f'{label}: поле не встретилось; удаление не подтверждено.', left, right, context, field)
            elif set(a['types']) != set(b['types']):
                add(key, 'type', f"{label}: типы {', '.join(a['types'])} → {', '.join(b['types'])}.", left, right, context, field)
    for change in changes:
        values = []
        for side in ('before', 'after'):
            operation = change[side]
            stat = next((f for f in operation['fields'] if f['context'] == change['context']
                         and f['name'] == change['field']), None) if operation and change['field'] else None
            values.append({'count': operation['count'] if operation else 0,
                           'types': ', '.join(sorted(stat['types'])) if stat else None,
                           'present': stat['count'] if stat else 0, 'total': stat['total'] if stat else 0,
                           'statuses': operation['statuses'] if operation else []})
        change['values'] = values
        change['smaller_sample'] = values[0]['count'] >= 10 and values[1]['count'] < values[0]['count'] / 2
    return changes
