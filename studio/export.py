"""Extract identical object schemas into reusable OpenAPI components on export."""

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json


def reusable_schemas(document):
    document = deepcopy(document)
    counts, schemas = Counter(), {}

    def fingerprint(schema):
        return json.dumps(schema, sort_keys=True, separators=(',', ':'))

    def children(schema):
        if not isinstance(schema, dict):
            return []
        result = list(schema.get('properties', {}).values())
        for key in ('items', 'additionalProperties'):
            if isinstance(schema.get(key), dict):
                result.append(schema[key])
        for key in ('anyOf', 'oneOf', 'allOf'):
            result.extend(schema.get(key, []))
        return result

    roots = []
    for methods in document['paths'].values():
        for operation in methods.values():
            roots.extend(p['schema'] for p in operation.get('parameters', []) if 'schema' in p)
            bodies = [operation.get('requestBody', {}), *operation.get('responses', {}).values()]
            roots.extend(media['schema'] for body in bodies for media in body.get('content', {}).values() if 'schema' in media)

    def count(schema):
        if schema.get('type') == 'object' and schema.get('properties'):
            key = fingerprint(schema)
            counts[key] += 1
            schemas[key] = schema
        for child in children(schema):
            count(child)
    for root in roots:
        count(root)
    names = {key: 'Model_' + sha256(key.encode()).hexdigest()[:16] for key, number in counts.items() if number > 1}

    def replace(schema, skip=None):
        key = fingerprint(schema)
        if key in names and key != skip:
            schema.clear()
            schema['$ref'] = '#/components/schemas/' + names[key]
            return
        for child in children(schema):
            replace(child)
    definitions = {names[key]: deepcopy(schemas[key]) for key in names}
    for key, name in names.items():
        replace(definitions[name], skip=key)
    for root in roots:
        replace(root)
    if definitions:
        document.setdefault('components', {})['schemas'] = definitions
    return document
