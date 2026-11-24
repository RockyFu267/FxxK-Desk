"""Validated local UI configuration; preserve unrelated TOML sections and secrets."""
import json
import re
import tomllib
from pathlib import Path

from .relay import load_route


def patch_sections(text, changes):
    for section, values in changes.items():
        header = re.compile(r'^\[' + re.escape(section) + r'\]\s*$', re.M)
        match = header.search(text)
        if not match:
            text += '\n[' + section + ']\n'
            match = header.search(text)
        end = re.search(r'^\[', text[match.end():], re.M)
        stop = match.end() + end.start() if end else len(text)
        body = text[match.end():stop]
        for key, value in values.items():
            line = key + ' = ' + json.dumps(value, ensure_ascii=False)
            pattern = re.compile(r'^' + re.escape(key) + r'\s*=.*$', re.M)
            body = pattern.sub(lambda _: line, body) if pattern.search(body) else body.rstrip() + '\n' + line + '\n'
        text = text[:match.end()] + body + text[stop:]
    return text


class ConsoleConfig:
    def __init__(self, path='config.local.toml', route=None):
        self.path = Path(path)
        self.data = tomllib.loads(self.path.read_text(encoding='utf-8'))
        self.route = route or next(iter(self.data.get('relays', {})), 'ops_to_test')
        self.routing, self.source, self.destination = load_route(self.path, self.route)

    def save(self, source, destination, executable, window_title, auto_send, advanced):
        if not source.strip() or not destination.strip() or source.strip() == destination.strip():
            raise ValueError('监控群和通知群必须填写，且不能相同')
        changes = {}
        for name, chat in ((self.routing['source'], source), (self.routing['destination'], destination)):
            changes['targets.' + name] = {**advanced, 'chat': chat.strip(),
                                         'executable': executable.strip(), 'window_title': window_title.strip()}
        changes['relays.' + self.route] = {'auto_send': bool(auto_send)}
        text = patch_sections(self.path.read_text(encoding='utf-8'), changes)
        temporary = self.path.with_name(self.path.name + '.tmp')
        try:
            temporary.write_text(text, encoding='utf-8')
            load_route(temporary, self.route)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.__init__(self.path, self.route)
