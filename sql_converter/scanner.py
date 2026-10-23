"""Split SQL on statement delimiters, not on strings or comments.

Deliberately conservative: dollar-quoted bodies are kept together so that
unsupported PL/pgSQL cannot accidentally be emitted as separate statements.
"""
from __future__ import annotations
from dataclasses import dataclass
import re

@dataclass(frozen=True)
class Statement:
    sql: str
    line: int
    too_large: bool = False
    unterminated: bool = False


_NAVICAT_EMPTY_LITERAL_PREFIX = re.compile(
    r'(?i)(?<![A-Za-z0-9_$])_(?:utf8mb4|utf8mb3|utf8)\\$'
)


def is_navicat_escaped_empty_literal(before: str, after: str, *, postgres_mode: bool = False) -> bool:
    """Detect only a paired, charset-introduced Navicat empty-string escape.

    `before` ends before the first apostrophe, while `after` starts directly
    after it. This must not apply inside quoted strings or in OpenGauss SQL.
    """
    return bool(not postgres_mode and after.startswith("\\'") and
                _NAVICAT_EMPTY_LITERAL_PREFIX.search(before[-40:]))


def split_sql(text: str, *, postgres_mode: bool = False) -> list[Statement]:
    statements: list[Statement] = []
    start = 0
    start_line = 1
    line = 1
    i = 0
    state = 'normal'
    dollar_tag = ''
    escaped_string = True
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ''
        if c == '\n':
            line += 1
        if state == 'normal':
            if c == "'":
                if is_navicat_escaped_empty_literal(text[max(0, i - 40):i], text[i + 1:i + 3],
                                                    postgres_mode=postgres_mode):
                    i += 3  # Both paired apostrophes are lexical data, not delimiters.
                    continue
                state = 'single'
                escaped_string = not postgres_mode or (i > 0 and text[i-1] in 'Ee' and (i == 1 or not (text[i-2].isalnum() or text[i-2] in '_$'))) 
            elif c == '"':
                state = 'double'
            elif c == '`':
                state = 'backtick'
            elif c == '-' and nxt == '-' and (i + 2 >= len(text) or text[i+2].isspace()):
                state = 'line_comment'; i += 1
            elif c == '#':
                state = 'line_comment'
            elif c == '/' and nxt == '*':
                state = 'block_comment'; i += 1
            elif c == '$':
                m = re.match(r'\$[a-zA-Z_0-9]*\$', text[i:])
                if m:
                    dollar_tag = m.group()
                    state = 'dollar'; i += len(dollar_tag) - 1
            elif c == ';':
                fragment = text[start:i+1]
                if fragment.strip():
                    statements.append(Statement(fragment, start_line))
                start = i + 1
                start_line = line
        elif state in ('single', 'double', 'backtick'):
            quote = {'single':"'", 'double':'"', 'backtick':'`'}[state]
            if c == '\\' and (state == 'single' and escaped_string or state == 'double' and not postgres_mode) and i + 1 < len(text):
                # MySQL-style escaping. Detect and flag unsafe payloads later.
                if text[i+1] == '\n':
                    line += 1
                i += 1
            elif c == quote:
                if nxt == quote:
                    i += 1  # SQL-standard doubled quote
                else:
                    state = 'normal'
        elif state == 'line_comment':
            if c == '\n':
                state = 'normal'
        elif state == 'block_comment':
            if c == '*' and nxt == '/':
                i += 1; state = 'normal'
        elif state == 'dollar':
            if text.startswith(dollar_tag, i):
                i += len(dollar_tag) - 1
                state = 'normal'
        i += 1
    if text[start:].strip():
        statements.append(Statement(text[start:], start_line))
    return statements


def split_top_level(text: str) -> list[str]:
    """Split a table body by commas outside nested expressions or quotes."""
    output: list[str] = []
    depth = 0
    start = 0
    state = 'normal'
    i = 0
    while i < len(text):
        c = text[i]
        nxt = text[i+1] if i+1 < len(text) else ''
        if state == 'normal':
            if c in "'\"`": state = c
            elif c == '(' : depth += 1
            elif c == ')': depth -= 1
            elif c == ',' and depth == 0:
                output.append(text[start:i].strip()); start = i + 1
        elif c == '\\' and i+1 < len(text):
            i += 1
        elif c == state:
            if nxt == state: i += 1
            else: state = 'normal'
        i += 1
    output.append(text[start:].strip())
    return output


def matched_paren(text: str, begin: int) -> int:
    """Return matching right parenthesis index, respecting SQL quotes."""
    depth = 0
    state = 'normal'
    i = begin
    while i < len(text):
        c = text[i]
        nxt = text[i+1] if i+1 < len(text) else ''
        if state == 'normal':
            if c in "'\"`": state = c
            elif c == '(': depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0: return i
        elif c == '\\' and i+1 < len(text): i += 1
        elif c == state:
            if nxt == state: i += 1
            else: state = 'normal'
        i += 1
    return -1


# Event-based identifier replacement avoids Python loops over long INSERT bodies.
_ID_EVENTS = {
    'mysql_to_opengauss': re.compile(r"[`'/-]"),
    'opengauss_to_mysql': re.compile(r'["\'/-]'),
}
_SINGLE_EVENTS = re.compile(r"['\\]")


def convert_identifier_quotes(sql: str, direction: str) -> str:
    """Change only identifier quotes; never replace tokens inside strings/comments."""
    source, target = ('`', '"') if direction == 'mysql_to_opengauss' else ('"', '`')
    if source not in sql:
        return sql
    out: list[str] = []
    pos = 0
    n = len(sql)
    state = 'normal'
    events = _ID_EVENTS[direction]
    while pos < n:
        if state == 'normal':
            m = events.search(sql, pos)
            if m is None:
                out.append(sql[pos:])
                break
            x = m.start()
            out.append(sql[pos:x])
            c = sql[x]
            nxt = sql[x + 1] if x + 1 < n else ''
            if c == "'":
                out.append(c)
                state = 'single'
                pos = x + 1
            elif c == source:
                out.append(target)
                state = 'identifier'
                pos = x + 1
            elif c == '-' and nxt == '-' and (x + 2 == n or sql[x + 2].isspace()):
                out.append('--')
                state = 'line_comment'
                pos = x + 2
            elif c == '/' and nxt == '*':
                out.append('/*')
                state = 'block_comment'
                pos = x + 2
            else:
                out.append(c)
                pos = x + 1
        elif state == 'single':
            m = _SINGLE_EVENTS.search(sql, pos)
            if m is None:
                out.append(sql[pos:])
                break
            x = m.start()
            out.append(sql[pos:x])
            c = sql[x]
            nxt = sql[x + 1] if x + 1 < n else ''
            if c == '\\' and x + 1 < n:
                out.append(sql[x:x + 2])
                pos = x + 2
            elif c == "'" and nxt == "'":
                out.append("''")
                pos = x + 2
            else:
                out.append(c)
                pos = x + 1
                if c == "'":
                    state = 'normal'
        elif state == 'identifier':
            x = sql.find(source, pos)
            # A literal target quote in an identifier must be escaped.
            y = sql.find(target, pos)
            candidates = [a for a in (x, y) if a >= 0]
            if not candidates:
                out.append(sql[pos:])
                break
            i = min(candidates)
            out.append(sql[pos:i])
            if sql[i] == source:
                if i + 1 < n and sql[i + 1] == source:
                    out.append(target)
                    pos = i + 2
                else:
                    out.append(target)
                    pos = i + 1
                    state = 'normal'
            else:
                out.append(target * 2)
                pos = i + 1
        elif state == 'line_comment':
            end = sql.find('\n', pos)
            if end < 0:
                out.append(sql[pos:])
                break
            out.append(sql[pos:end + 1])
            pos = end + 1
            state = 'normal'
        else:
            end = sql.find('*/', pos)
            if end < 0:
                out.append(sql[pos:])
                break
            out.append(sql[pos:end + 2])
            pos = end + 2
            state = 'normal'
    return ''.join(out)


def strip_leading_comments(sql: str) -> str:
    """Find executable token after leading plain comments."""
    s = sql.lstrip()
    while True:
        if s.startswith('--') or s.startswith('#'):
            pos = s.find('\n')
            if pos < 0: return ''
            s = s[pos+1:].lstrip()
        elif s.startswith('/*') and not s.startswith('/*!'):
            pos = s.find('*/')
            if pos < 0: return ''
            s = s[pos+2:].lstrip()
        else:
            return s


def has_unsafe_string_escapes(sql: str, direction: str) -> bool:
    """Flag nonportable string escapes rather than silently rewriting data."""
    if direction == 'opengauss_to_mysql' and re.search(r"\bE\s*'", sql, re.I):
        return True
    state = 'normal'; i = 0
    while i < len(sql):
        c=sql[i]; nxt=sql[i+1] if i+1 < len(sql) else ''
        if state == 'normal':
            if c == "'": state = 'single'
            elif c == '"': state = 'double'
            elif c == '`': state = 'backtick'
            elif c == '-' and nxt == '-' and (i+2 == len(sql) or sql[i+2].isspace()): state='comment'
            elif c == '/' and nxt == '*': state='block'; i+=1
        elif state == 'single':
            if c == '\\' and nxt:
                return True
            if c == "'":
                if nxt == "'": i+=1
                else: state = 'normal'
        elif state in ('double','backtick'):
            if c == ('"' if state=='double' else '`'):
                if nxt == c: i+=1
                else: state = 'normal'
        elif state == 'comment' and c == '\n': state='normal'
        elif state == 'block' and c == '*' and nxt == '/': state='normal'; i+=1
        i+=1
    return False
