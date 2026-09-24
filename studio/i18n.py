"""Explicit UI translations; user-provided names and captured values are never translated."""

import json
from pathlib import Path
import re

from flask import has_request_context, session

EN = json.loads((Path(__file__).parent / 'locales/en.json').read_text())
RU = {'queued': 'В очереди', 'running': 'Обработка', 'done': 'Готово', 'failed': 'Ошибка'}


def language():
    return session.get('language', 'ru') if has_request_context() else 'ru'


def translate(message, **values):
    translated = EN.get(message, message) if language() == 'en' else RU.get(message, message)
    return translated.format(**values) if values else translated


def error_message(message):
    if language() != 'en' or message in EN:
        return translate(message)
    match = re.fullmatch(r'(.+): файл не содержит корректный JSON\.', message)
    if match:
        return f'{match[1]}: the file does not contain valid JSON.'
    match = re.fullmatch(r'(.+): некорректная структура HAR\. (.+)', message)
    if match:
        return f'{match[1]}: invalid HAR structure. {match[2]}'
    for source, translated in EN.items():
        if message.endswith(': ' + source):
            return message[:-len(source)] + translated
    if re.search('[А-Яа-яЁё]', message):
        return 'Unable to process this data. Check the selected file and try again.'
    return message


def change_message(change):
    if language() == 'ru':
        return change['message']
    if change['field']:
        context, field = change['context'], change['field']
        label = f'{context} → {field}'
        if 'тело этого типа' in change['message'] or 'тела этого типа' in change['message']:
            key = ('{context}: впервые наблюдалось тело этого типа.' if change['kind'] == 'new'
                   else '{context}: тело этого типа не встретилось в новой выборке.')
            return translate(key, context=context)
        if change['kind'] == 'type':
            return translate('{label}: типы {before} → {after}.', label=label,
                             before=change['values'][0]['types'], after=change['values'][1]['types'])
        return translate('{label}: поле впервые наблюдалось.' if change['kind'] == 'new' else
                         '{label}: поле не встретилось; удаление не подтверждено.', label=label)
    match = re.fullmatch(r'Впервые наблюдался ответ (.+)\.', change['message'])
    if match:
        return translate('Впервые наблюдался ответ {status}.', status=match[1])
    match = re.fullmatch(r'Ответ (.+) не встретился в новой выборке\.', change['message'])
    if match:
        return translate('Ответ {status} не встретился в новой выборке.', status=match[1])
    return translate(change['message'])
