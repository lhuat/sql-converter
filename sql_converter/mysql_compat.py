"""MySQL dump normalization for openGauss.

Scope: lexically safe MySQL string literals / charset introducers, plus a
strictly bounded CASE WHEN index expression subset. Unknown forms must be
rejected, not guessed. No SQL is ever executed.
"""
from __future__ import annotations
import re

class LiteralConversionError(ValueError):
    pass

# MySQL string escape sequences documented for backslash escape mode.
_INTRODUCER = re.compile(r"(?:_utf8mb4|_utf8mb3|_utf8|N)(?=\s*')", re.I)

_ESC = {'0': '\0', "'": "'", '"':'"', 'b':'\b', 'n':'\n', 'r':'\r', 't':'\t', 'Z':'\x1a', '\\':'\\'}


def _read_mysql_single(text: str, start: int) -> tuple[str, int]:
    """Return decoded string + exclusive end offset starting at an apostrophe."""
    # Common dump values contain neither escaping nor embedded apostrophes.
    # The fast path uses C string-search instead of one Python step per byte.
    plain_end = text.find("'", start + 1)
    if plain_end >= 0 and '\\' not in text[start + 1:plain_end] and (plain_end + 1 == len(text) or text[plain_end + 1] != "'"):
        return text[start + 1:plain_end], plain_end + 1
    i = start + 1
    out = []
    while i < len(text):
        c = text[i]
        if c == "'":
            if i + 1 < len(text) and text[i + 1] == "'":
                out.append("'"); i += 2; continue
            return ''.join(out), i + 1
        if c == '\\':
            if i + 1 >= len(text):
                raise LiteralConversionError('字符串以不完整的反斜杠转义结尾')
            nxt = text[i + 1]
            # MySQL removes a slash on other unknown sequences except \\% and \\_.
            out.append(('\\' + nxt) if nxt in ('%', '_') else _ESC.get(nxt, nxt))
            i += 2; continue
        out.append(c)
        i += 1
    raise LiteralConversionError('字符串缺少闭合单引号')


def _target_literal(text: str) -> str:
    if '\0' in text:
        raise LiteralConversionError('源字符串包含 NUL，openGauss 文本类型不能存储零字节')
    if '\x1a' in text:
        raise LiteralConversionError('源字符串包含 Ctrl+Z 字符，需明确二进制/文本存储策略')
    # E'' ensures backslashes / tabs / CRLF retain exact meaning regardless of
    # target standard_conforming_strings setting. SQL-standard quote doubling.
    if any(c in text for c in ('\\', '\n', '\r', '\t', '\b')):
        esc = (text.replace('\\', '\\\\').replace('\r', '\\r').replace('\n', '\\n')
                   .replace('\t', '\\t').replace('\b', '\\b').replace("'", "''"))
        return "E'" + esc + "'"
    return "'" + text.replace("'", "''") + "'"


_LITERAL_EVENTS = re.compile(r"['\"`/#-]|[Nn_]")


def normalize_mysql_literals(sql: str) -> tuple[str, bool]:
    """Safely rewrite MySQL literals by jumping between lexical events.

    The ordinary part of a large INSERT is copied with C-level slicing, while
    strings and comments still have strict, stateful parsing and escaping.
    """
    out: list[str] = []
    i = 0
    size = len(sql)
    touched = False
    while i < size:
        match = _LITERAL_EVENTS.search(sql, i)
        if match is None:
            out.append(sql[i:])
            break
        index = match.start()
        out.append(sql[i:index])
        c = sql[index]
        nxt = sql[index+1] if index+1<size else ''
        if c == '-' and nxt == '-' and (index+2 >= size or sql[index+2].isspace()):
            end = sql.find('\n', index+2)
            if end < 0:
                out.append(sql[index:])
                break
            out.append(sql[index:end+1])
            i = end + 1
        elif c == '#':
            end = sql.find('\n', index+1)
            if end < 0:
                out.append(sql[index:])
                break
            out.append(sql[index:end+1])
            i = end + 1
        elif c == '/' and nxt == '*':
            end = sql.find('*/', index+2)
            end = size if end < 0 else end+2
            out.append(sql[index:end])
            i = end
        elif c in ('`','"'):
            q = c
            pos = index+1
            while pos < size:
                end = sql.find(q, pos)
                if q == '"':
                    escape = sql.find('\\', pos)
                    if escape >= 0 and (end < 0 or escape < end):
                        pos = min(escape+2,size)
                        continue
                if end < 0:
                    pos = size
                    break
                if end + 1 < size and sql[end+1] == q:
                    pos = end + 2
                    continue
                pos = end + 1
                break
            out.append(sql[index:pos])
            i = pos
        elif c in ('_','N','n'):
            match = _INTRODUCER.match(sql,index)
            if match and (index==0 or not (sql[index-1].isalnum() or sql[index-1] in '_$')):
                i = match.end()
                touched = True
            else:
                out.append(c)
                i = index+1
        elif c == "'":
            value, end = _read_mysql_single(sql, index)
            replacement = _target_literal(value)
            touched = touched or replacement != sql[index:end]
            out.append(replacement)
            i = end
        else:
            out.append(c)
            i = index+1
    return ''.join(out), touched


def _enclosing_parens(s: str) -> bool:
    s=s.strip()
    if not(s.startswith('(') and s.endswith(')')):return False
    i=0;depth=0;state=None
    while i<len(s):
        c=s[i]
        if state:
            if c==state:
                if i+1<len(s) and s[i+1]==c:i+=1
                else:state=None
        elif c=="'":state="'"
        elif c in '"`':state=c
        elif c=='(':depth+=1
        elif c==')':
            depth-=1
            if depth==0 and i<len(s)-1:return False
            if depth<0:return False
        i+=1
    return depth==0


def unwrap_parens(expr: str) -> str:
    e=expr.strip()
    while _enclosing_parens(e):e=e[1:-1].strip()
    return e


def validate_case_index_expression(expr: str) -> str:
    """Accept a deterministic CASE WHEN ... THEN ... ELSE NULL expression.

    Only the boolean and deterministic scalar forms present in tested dumps are
    allowed. This prevents a regex blanket rewrite of arbitrary executable SQL.
    """
    e=unwrap_parens(expr)
    m=re.fullmatch(r'CASE\s+WHEN\s+(.+?)\s+THEN\s+(.+?)\s+ELSE\s+NULL\s+END',e,re.I|re.S)
    if not m:raise LiteralConversionError('不支持的表达式索引：仅支持经校验的 CASE WHEN ... ELSE NULL END')
    condition,result=m.groups()
    # Only numeric/text/NULL predicates and basic AND/OR; no subqueries or
    # user-defined functions. Use word-based bans before identifier conversion.
    for s in (condition,result):
        if re.search(r';|--|/\*|\*/|\b(?:SELECT|INSERT|UPDATE|DELETE|UNION|EXISTS|FROM|CALL|SLEEP)\b|::|->|:=',s,re.I):
            raise LiteralConversionError('条件唯一索引包含不支持的运算符或子查询')
        if not re.fullmatch(r'[\w`"\s().,<>=!+\-\']*',s):
            raise LiteralConversionError('条件唯一索引包含未经验证的表达式字符')
        functions=re.findall(r'(?<![\w`])([A-Za-z_]\w*)\s*\(',s)
        if any(f.upper() not in ('LOWER','TRIM','AND','OR','NOT') for f in functions):
            raise LiteralConversionError('条件唯一索引包含未经验证的函数')
    # Result can be a simple column, integer, lower(trim(col)), with parentheses.
    res=unwrap_parens(result)
    res_ok=(re.fullmatch(r'[\w`"$]+',res) or
            re.fullmatch(r'(?i)LOWER\s*\(\s*TRIM\s*\(\s*[`"\w$]+\s*\)\s*\)',res))
    if not res_ok:
        raise LiteralConversionError('条件唯一索引 THEN 表达式不是允许的简单列、常量或 LOWER(TRIM(col))')
    # Condition token stream allow quoted idents, numbers, empty quoted string,
    # comparison operators, IS [NOT] NULL, AND/OR, brackets.
    masked=re.sub(r"E?'(?:''|[^'])*'", "''", condition,flags=re.I)
    if not re.fullmatch(r'[\w`"\s().,<>=!+\-\']*',masked):
        raise LiteralConversionError('条件唯一索引条件中有不支持的字符')
    # SQL keywords are normalized only outside quoted values here.
    e = re.sub(r'(?i)\b(case|when|then|else|end)\b', lambda m:m.group().upper(), e)
    return '('+e+')'
