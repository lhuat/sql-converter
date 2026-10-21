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


def split_sql(text: str) -> list[Statement]:
    statements: list[Statement] = []
    start = 0
    start_line = 1
    line = 1
    i = 0
    state = 'normal'
    dollar_tag = ''
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ''
        if c == '\n':
            line += 1
        if state == 'normal':
            if c == "'":
                state = 'single'
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
            if c == '\\' and state in ('single', 'double') and i + 1 < len(text):
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


def convert_identifier_quotes(sql: str, direction: str) -> str:
    """Only identifier delimiters change; quoted literals/comments are untouched."""
    source, target = ('`','"') if direction == 'mysql_to_opengauss' else ('"','`')
    out: list[str] = []
    i = 0
    state = 'normal'
    while i < len(sql):
        c = sql[i]; nxt = sql[i+1] if i + 1 < len(sql) else ''
        if state == 'normal':
            if c == "'": state = 'single'; out.append(c)
            elif c == source: state = 'identifier'; out.append(target)
            elif c == '-' and nxt == '-' and (i+2 == len(sql) or sql[i+2].isspace()):
                state='line_comment'; out.extend([c,nxt]); i += 1
            elif c == '/' and nxt == '*':
                state='block_comment'; out.extend([c,nxt]); i += 1
            else: out.append(c)
        elif state == 'single':
            out.append(c)
            if c == '\\' and i+1 < len(sql): i+=1; out.append(sql[i])
            elif c == "'":
                if nxt == "'": i+=1; out.append(nxt)
                else: state='normal'
        elif state == 'identifier':
            if c == source:
                if nxt == source:
                    out.append(target); i += 1  # SQL identifier escaped delimiter
                else: out.append(target); state='normal'
            elif c == target:
                out.append(target + target)  # escape a literal target character in identifier
            else: out.append(c)
        elif state == 'line_comment':
            out.append(c)
            if c == '\n': state='normal'
        elif state == 'block_comment':
            out.append(c)
            if c == '*' and nxt == '/': i+=1; out.append(nxt); state='normal'
        i += 1
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
