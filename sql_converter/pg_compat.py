"""PostgreSQL/openGauss -> MySQL literal and safe cast adapters.

No raw global regex replacements on SQL payloads: comments, identifiers and
single-quoted values are parsed separately. This module assumes the usual
standard_conforming_strings=on; the caller must reject scripts that disable it.
"""
from __future__ import annotations
import re

class PostgresLiteralError(ValueError):
    pass

_SIMPLE_ESC = {'n':'\n','r':'\r','t':'\t','b':'\b','f':'\f','v':'\v','a':'\a',"'":"'",'"':'"','\\':'\\'}
_EVENT = re.compile(r"(?:[Ee](?=')|['\"/\-])")


def _parse_quoted(sql: str, start: int, extended: bool) -> tuple[str, int]:
    i = start + 1
    end = len(sql)
    out = []
    while i < end:
        c = sql[i]
        if c == "'":
            if i + 1 < end and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return ''.join(out), i + 1
        if extended and c == '\\':
            if i + 1 >= end:
                raise PostgresLiteralError('E 字符串中存在不完整的反斜杠转义')
            nxt = sql[i + 1]
            if nxt in _SIMPLE_ESC:
                out.append(_SIMPLE_ESC[nxt]); i += 2; continue
            if nxt in '01234567':
                m = re.match(r'[0-7]{1,3}', sql[i+1:i+4])
                assert m is not None
                out.append(chr(int(m.group(), 8))); i += 1 + len(m.group()); continue
            if nxt in ('x','X'):
                m = re.match(r'[a-fA-F0-9]{1,2}', sql[i+2:i+4])
                if not m: raise PostgresLiteralError('E 字符串包含无效的十六进制转义')
                out.append(chr(int(m.group(),16))); i += 2 + len(m.group()); continue
            if nxt in ('u', 'U'):
                digits = 4 if nxt == 'u' else 8
                hexdigits = sql[i+2:i+2+digits]
                if len(hexdigits) != digits or not re.fullmatch(r'[0-9a-fA-F]+', hexdigits):
                    raise PostgresLiteralError('E 字符串包含无效 Unicode 转义')
                cp = int(hexdigits,16)
                if cp > 0x10ffff or 0xd800 <= cp <= 0xdfff:
                    raise PostgresLiteralError('E 字符串 Unicode 码点无效')
                out.append(chr(cp)); i += 2+digits; continue
            raise PostgresLiteralError(f'未知的 PostgreSQL E 字符串转义：\\{nxt}')
        out.append(c)
        i += 1
    raise PostgresLiteralError('SQL 字符串缺少结束单引号')


def _mysql_literal(value: str) -> str:
    """Use MySQL default backslash-escape SQL mode; separate SQL_MODE guard."""
    replacements = {'\\':'\\\\', "'":"''", '\0':'\\0', '\n':'\\n', '\r':'\\r', '\t':'\\t',
                    '\b':'\\b', '\x1a':'\\Z'}
    return "'" + ''.join(replacements.get(c, c) for c in value) + "'"


def normalize_postgres_literals(sql: str) -> tuple[str, bool]:
    """Re-quote PostgreSQL ordinary/E strings into MySQL-safe literals."""
    output = []
    i = 0
    changed = False
    n = len(sql)
    while i < n:
        match = _EVENT.search(sql, i)
        if not match:
            output.append(sql[i:]); break
        j = match.start()
        output.append(sql[i:j])
        c = sql[j]
        nxt = sql[j+1] if j+1<n else ''
        if c=='-' and nxt=='-' and (j+2>=n or sql[j+2].isspace()):
            end=sql.find('\n',j+2)
            end=n if end<0 else end+1
            output.append(sql[j:end]);i=end;continue
        if c=='/' and nxt=='*':
            end=sql.find('*/',j+2)
            if end<0: raise PostgresLiteralError('SQL 块注释没有正确结束')
            end+=2
            output.append(sql[j:end]);i=end;continue
        if c=='"':
            pos=j+1
            while True:
                end=sql.find('"',pos)
                if end<0:raise PostgresLiteralError('SQL 双引号标识符没有正确结束')
                if end+1<n and sql[end+1]=='"':pos=end+2;continue
                output.append(sql[j:end+1]);i=end+1;break
            continue
        extended=(c in 'Ee' and nxt=="'" and (j==0 or not (sql[j-1].isalnum() or sql[j-1] in '_$')))
        if extended or c=="'":
            start=j+1 if extended else j
            value,end=_parse_quoted(sql,start,extended)
            rewritten=_mysql_literal(value)
            original=sql[j:end]
            output.append(rewritten)
            changed |= rewritten != original
            i=end;continue
        output.append(c);i=j+1
    return ''.join(output),changed


_CAST_RE = re.compile(
    r'(?P<expr>(?:`[^`]+`|"[^"]+"|\b\d+\b))\s*::\s*'
    r'(?P<type>INT(?:EGER)?|SMALLINT|BIGINT|TEXT|VARCHAR(?:\(\d+\))?|CHAR(?:\(\d+\))?|'
    r'NUMERIC(?:\(\d+(?:,\d+)?\))?|DECIMAL(?:\(\d+(?:,\d+)?\))?|DATE|TIMESTAMP(?:\(\d+\))?)\b',
    re.I)

def rewrite_simple_postgres_casts(sql: str) -> str:
    """Bounded cast rewrite for unambiguous identifier/number casts only.

    Casts on complex expressions/literals remain blocked rather than guessed.
    """
    def cast(m: re.Match) -> str:
        typ=m.group('type').upper()
        typ=re.sub(r'^INTEGER$', 'SIGNED', typ)
        typ=re.sub(r'^(INT|SMALLINT|BIGINT)$', 'SIGNED', typ)
        if typ.startswith('TEXT'):typ='CHAR'
        if typ.startswith('TIMESTAMP'):typ=re.sub('TIMESTAMP','DATETIME',typ)
        return f'CAST({m.group("expr")} AS {typ})'
    return _CAST_RE.sub(cast,sql)
