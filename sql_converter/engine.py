"""Conservative offline MySQL <-> openGauss dump converter.

The converter intentionally does not claim full SQL dialect compatibility.
Unsupported or ambiguous statements are replaced with conspicuous *SQL
comments*, and the JSON report marks them as blockers. Never execute the
output without reviewing blockers and testing it against a disposable DB.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
import json
import re
import codecs
import os
import tempfile
import time

from .mysql_compat import normalize_mysql_literals, validate_case_index_expression, LiteralConversionError
from .mappings import replace_type
from .streaming import (MAX_SQL_FILE_BYTES, MAX_STATEMENT_BYTES, validate_file_size,
                        detect_encoding, find_unsafe_dump_directive, iter_sql_stream, preflight_sql)
from .scanner import (Statement, convert_identifier_quotes, has_unsafe_string_escapes,
                      matched_paren, split_sql, split_top_level, strip_leading_comments)

DIRECTIONS = ('mysql_to_opengauss', 'opengauss_to_mysql')
SAFE_GENERIC_TYPES = re.compile(
    r'^(?:BIGINT|INTEGER|INT|SMALLINT|REAL|FLOAT|DOUBLE(?:\s+PRECISION)?|NUMERIC|DECIMAL|'
    r'CHAR|CHARACTER|VARCHAR|DATE|TIME|TIMESTAMP|BOOLEAN|BOOL|TEXT|JSON|JSONB|BYTEA|'
    r'BLOB|BINARY|VARBINARY|UUID|SERIAL|BIGSERIAL)(?!\w)', re.I)
SENSITIVE_SQL = re.compile(
    r'\b(?:ON\s+DUPLICATE\s+KEY|ON\s+CONFLICT|INSERT\s+IGNORE|REPLACE\s+INTO|'
    r'RETURNING|SQL_CALC_FOUND_ROWS|LOAD\s+DATA|COPY\b|DELIMITER\b|'
    r'PARTITION\s+BY|CREATE\s+(?:EVENT|TRIGGER|PROCEDURE|FUNCTION)|'
    r'EXECUTE\s+IMMEDIATE|DO\s+\$|LOCK\s+TABLES|UNLOCK\s+TABLES)\b', re.I)
DANGEROUS_COLUMN_ATTRIBUTES = re.compile(
    r'\b(?:COLLATE|CHARACTER\s+SET|ON\s+UPDATE|STORED|VIRTUAL|'
    r'GENERATED|REFERENCES|ZEROFILL|ENUM|SET|ARRAY)\b|::', re.I)

@dataclass
class Finding:
    severity: str
    code: str
    line: int
    message: str
    excerpt: str = ''

@dataclass
class Report:
    source_database: str
    target_database: str
    original_filename: str = ''
    total: int = 0
    converted: int = 0
    skipped: int = 0
    blocked: int = 0
    findings: list[Finding] = field(default_factory=list)
    omitted_findings: int = 0
    metrics: dict[str, float | int] = field(default_factory=dict)
    _seen: set[tuple[str, str, int, str]] = field(default_factory=set, repr=False)

    @property
    def ready(self) -> bool:
        return self.blocked == 0

    def add(self, severity: str, code: str, line: int, message: str, excerpt: str = '') -> None:
        key = (severity, code, line, message)
        if key in self._seen:
            return
        # Avoid unbounded report memory growth for hundreds of thousands of
        # unsupported statements. Counts remain complete; excerpts are capped.
        if len(self.findings) >= 5000:
            self.omitted_findings += 1
            return
        self._seen.add(key)
        self.findings.append(Finding(severity, code, line, message, excerpt[:350]))

    def to_dict(self) -> dict:
        from collections import Counter
        summary = Counter(f'{f.severity}:{f.code}' for f in self.findings)
        return {
            'finding_summary': dict(sorted(summary.items())),
            'source_database': self.source_database,
            'target_database': self.target_database,
            'original_filename': self.original_filename,
            'total': self.total,
            'converted': self.converted,
            'skipped': self.skipped,
            'blocked': self.blocked,
            'findings': [asdict(f) for f in self.findings],
            'omitted_findings': self.omitted_findings,
            'metrics': self.metrics,
            'status': 'READY_FOR_TESTING' if self.ready else 'MANUAL_REVIEW_REQUIRED',
        }

class ConversionCancelled(Exception):
    """User-requested stop; uncommitted temp files are removed."""


class UnsupportedSQL(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Rule:
    """Add a rule subclass to RULES to support an additional SQL category.

    Rule ordering matters: the first matching rule owns the full statement.
    Every rule must either safely translate or raise UnsupportedSQL.
    """
    def accepts(self, sql: str) -> bool:
        raise NotImplementedError

    def convert(self, sql: str, ctx: 'Context') -> str:
        raise NotImplementedError

@dataclass
class Context:
    direction: str
    mappings: dict[str, str]
    report: Report
    line: int

    def info(self, code: str, message: str) -> None:
        self.report.add('INFO', code, self.line, message)

    def warning(self, code: str, message: str) -> None:
        self.report.add('WARNING', code, self.line, message)

    def quotes(self, sql: str) -> str:
        return convert_identifier_quotes(sql, self.direction)


def _no_semicolon(sql: str) -> str:
    return sql.strip().removesuffix(';').rstrip()


_MASK_EVENTS = re.compile(r"['\"`/#-]")


def _masked_sql_literals(sql: str) -> str:
    """Mask SQL literals/comments in linear time with C-level substring search.

    Retains exact string length for token-position checks. The opening double
    quote remains visible because MySQL's double-quote mode needs validation.
    """
    out: list[str] = []
    n = len(sql)
    pos = 0
    while pos < n:
        m = _MASK_EVENTS.search(sql, pos)
        if m is None:
            out.append(sql[pos:])
            break
        i = m.start()
        out.append(sql[pos:i])
        c = sql[i]
        nxt = sql[i+1] if i+1<n else ''
        if c in ("'", '"', '`'):
            out.append('"' if c == '"' else ' ')
            j = i + 1
            while j < n:
                close = sql.find(c, j)
                if c != '`':
                    backslash = sql.find('\\', j)
                    if backslash != -1 and (close == -1 or backslash < close):
                        j = min(backslash + 2, n)
                        continue
                if close < 0:
                    j = n
                    break
                if close+1<n and sql[close+1]==c:
                    j = close+2
                    continue
                j=close+1
                break
            out.append(' '*(j-i-1))
            pos=j
        elif c == '-' and nxt == '-' and (i+2>=n or sql[i+2].isspace()):
            end=sql.find('\n',i+2)
            if end<0:
                out.append(' '*(n-i))
                break
            out.append(' '*(end-i))
            out.append('\n')
            pos=end+1
        elif c == '#':
            end=sql.find('\n',i+1)
            if end<0:
                out.append(' '*(n-i))
                break
            out.append(' '*(end-i))
            out.append('\n')
            pos=end+1
        elif c == '/' and nxt == '*':
            end=sql.find('*/',i+2)
            end=n if end<0 else end+2
            out.append(' '*(end-i))
            pos=end
        else:
            out.append(c)
            pos=i+1
    return ''.join(out)


def _ensure_safe_expressions(sql: str, ctx: Context) -> None:
    # MySQL strings were normalized token-by-token before entering rules.
    # E'' is used to encode backslashes without relying on target session mode.
    if ctx.direction == 'opengauss_to_mysql' and has_unsafe_string_escapes(sql, ctx.direction):
        raise UnsupportedSQL('STRING_ESCAPE', '反向转换包含带转义的字符串，请使用专用处理器')
    code = _masked_sql_literals(sql)
    if re.search(r'\b0x[0-9a-f]+\b', code, re.I):
        raise UnsupportedSQL('BINARY_LITERAL', '十六进制数字字面量需要专用转换规则')
    # Check only prefixes in executable SQL; do not mistake the string 'b'
    # for a MySQL bit literal B'101'.
    for match in re.finditer(r"\b[XB](?=\s*')", sql, re.I):
        if code[match.start()] != ' ':
            raise UnsupportedSQL('BINARY_LITERAL', '二进制或十六进制字面量需要专用转换规则')
    if ctx.direction == 'mysql_to_opengauss' and '"' in code:
        # Distinguish executable double quotes from a harmless double-quote
        # character inside an ordinary single-quoted string.
        raise UnsupportedSQL('MYSQL_DOUBLE_QUOTE', 'MySQL 双引号可能表示字符串（取决于 ANSI_QUOTES），不能直接迁移')
    if re.search(r'\b(?:NOW\s*\(|IFNULL\s*\(|DATE_FORMAT\s*\(|TO_CHAR\s*\(|NVL\s*\(|IF\s*\()', code, re.I):
        raise UnsupportedSQL('DIALECT_FUNCTION', '包含数据库专有函数，请增加对应语法规则')
    if re.search(r'::\s*[A-Za-z_]', code):
        raise UnsupportedSQL('PG_CAST', 'PostgreSQL 风格类型强制转换需要专用规则')
    if re.search(r'\$[A-Za-z_0-9]*\$', code):
        raise UnsupportedSQL('DOLLAR_QUOTE', '包含 dollar-quoted SQL 代码块')


def _convert_mysql_index_columns(fields: str, ctx: Context) -> tuple[str, bool]:
    """Validate a MySQL index column list, including a narrow safe CASE form.

    Expressions must remain unique indexes, never UNIQUE constraints.
    """
    converted: list[str] = []
    expression_found = False
    for part in split_top_level(fields):
        part = part.strip()
        simple = re.fullmatch(r'([`\w$]+)(?:\s+(ASC|DESC))?', part, re.I)
        if simple:
            name, ordering = simple.groups()
            converted.append(_identifier(name, ctx) + (f' {ordering.upper()}' if ordering else ''))
            continue
        if re.match(r'[`\w$]+\s*\(\s*\d+\s*\)', part, re.I):
            raise UnsupportedSQL('PREFIX_INDEX', 'MySQL 前缀索引不能转换为完整列索引')
        # Deterministic, expression-based logical-delete uniqueness; preserve
        # null suppression, case conditions, and LOWER(TRIM()) semantics.
        order_match = re.search(r'\s+(ASC|DESC)\s*$', part, re.I)
        ordering = order_match.group(1) if order_match else None
        expression = part[:order_match.start()] if order_match else part
        try:
            normalized = validate_case_index_expression(expression)
        except LiteralConversionError as exc:
            raise UnsupportedSQL('INDEX_EXPRESSION', str(exc)) from exc
        converted.append(ctx.quotes(normalized) +
                         (' ' + ordering.upper() if ordering else ''))
        expression_found = True
    return (', '.join(converted) if expression_found else ','.join(converted)), expression_found


def _strip_mysql_column_collation(attributes: str, ctx: Context) -> str:
    """Remove MySQL-only column encoding declarations with explicit risk note."""
    charset = re.findall(r'\bCHARACTER\s+SET\s+([A-Za-z0-9_]+)', attributes, re.I)
    collate = re.findall(r'\bCOLLATE\s+([A-Za-z0-9_]+)', attributes, re.I)
    if not (charset or collate):
        return attributes
    if len(charset) > 1 or len(collate) > 1:
        raise UnsupportedSQL('COLUMN_COLLATION', '字段存在重复字符集或排序规则设置')
    if charset and charset[0].lower() not in ('utf8mb4', 'utf8', 'utf8mb3'):
        raise UnsupportedSQL('COLUMN_CHARSET', '非 UTF-8 字符集不能直接丢弃，需确认编码转换')
    if collate and not re.fullmatch(r'utf8(?:mb3|mb4)?_[A-Za-z0-9_]+', collate[0], re.I):
        raise UnsupportedSQL('COLUMN_COLLATION', '非 UTF-8 排序规则需要单独映射')
    attributes = re.sub(r'\s+CHARACTER\s+SET\s+[A-Za-z0-9_]+', '', attributes, flags=re.I)
    attributes = re.sub(r'\s+COLLATE\s+[A-Za-z0-9_]+', '', attributes, flags=re.I)
    ctx.warning('COLLATION_REVIEW', 'MySQL 字段字符集/排序规则已移除；请确认 openGauss 的大小写、排序和唯一性语义')
    return attributes

def _identifier(raw: str, ctx: Context) -> str:
    return ctx.quotes(raw.strip())


def _report_mapping_usage(ctx: Context, rule: str | None) -> None:
    if rule and rule in ctx.mappings:
        ctx.warning('CUSTOM_MAPPING', f'已使用自定义类型映射 {rule} → {ctx.mappings[rule]}；请确认目标类型语义')


class SkipSessionRule(Rule):
    def accepts(self, sql: str) -> bool:
        return bool(re.match(r'^(?:/\*!\d+\s+SET\b|SET\s+|USE\s+|LOCK\s+TABLES\b|UNLOCK\s+TABLES\b)', sql, re.I))

    def convert(self, sql: str, ctx: Context) -> str:
        if re.match(r'^/\*!\d+\s+SET\b', sql, re.I):
            ctx.info('DUMP_SESSION', '跳过源库导出工具的会话 SET 语句')
            return ''
        if re.match(r'^(?:LOCK\s+TABLES|UNLOCK\s+TABLES)', sql, re.I):
            ctx.info('DUMP_LOCK', '跳过导出期间使用的锁表语句')
            return ''
        if re.match(r'^USE\s+', sql, re.I):
            ctx.warning('USE_DATABASE', '跳过 USE 数据库语句；请在导入工具里选好目标数据库')
            return ''
        if re.match(r'^SET\s+', sql, re.I):
            # only known dump session settings can be dropped safely
            accepted = re.compile(
                r'^SET\s+(?:@@(?:SESSION\.|GLOBAL\.)?|SESSION\s+|GLOBAL\s+)?'
                r'(?:NAMES|character_set_client|character_set_results|collation_connection|'
                r'FOREIGN_KEY_CHECKS|UNIQUE_CHECKS|SQL_MODE|TIME_ZONE|'
                r'standard_conforming_strings|client_encoding|check_function_bodies|'
                r'search_path|statement_timeout|lock_timeout|idle_in_transaction_session_timeout|'
                r'row_security|default_tablespace|default_table_access_method|'
                r'xmloption|client_min_messages|extra_float_digits)\b', re.I)
            if not accepted.match(sql):
                raise UnsupportedSQL('UNKNOWN_SESSION_SETTING', '未知的 SET 会话语句，不能静默忽略')
            if re.search(r'\b(?:FOREIGN_KEY_CHECKS|UNIQUE_CHECKS)\b', sql, re.I) and re.search(r'=\s*0\s*;?$', sql):
                ctx.warning('CONSTRAINT_CHECK', '源脚本关闭了约束检查；目标库导入时应确认约束及数据顺序')
            else:
                ctx.info('DUMP_SESSION', '已忽略源数据库导出工具的普通会话配置')
            return ''
        raise UnsupportedSQL('SESSION', '未识别的会话语法')


class TransactionRule(Rule):
    def accepts(self, sql: str) -> bool:
        return bool(re.match(r'^(?:BEGIN(?:\s+WORK)?|START\s+TRANSACTION|COMMIT(?:\s+WORK)?|ROLLBACK(?:\s+WORK)?)\s*;?$', sql, re.I))

    def convert(self, sql: str, ctx: Context) -> str:
        val = sql.upper().strip().rstrip(';')
        return ('BEGIN;' if val.startswith(('START', 'BEGIN')) else 'COMMIT;' if val.startswith('COMMIT') else 'ROLLBACK;')


class CreateTableRule(Rule):
    def accepts(self, sql: str) -> bool:
        return bool(re.match(r'^CREATE\s+(?:TEMPORARY\s+)?TABLE\s+', sql, re.I))

    def convert(self, sql: str, ctx: Context) -> str:
        if SENSITIVE_SQL.search(sql):
            raise UnsupportedSQL('COMPLEX_CREATE_TABLE', '建表包含分区或其他需要专用处理的子语法')
        first = sql.find('(')
        if first < 0:
            raise UnsupportedSQL('CREATE_TABLE_PARSE', '找不到建表字段定义')
        last = matched_paren(sql, first)
        if last < 0:
            raise UnsupportedSQL('CREATE_TABLE_PARSE', '建表字段括号不匹配')
        _ensure_safe_expressions(sql, ctx)
        header = sql[:first].strip()
        tail = _no_semicolon(sql[last+1:])
        if not re.fullmatch(r'CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[`"\w$]+\.)?[`"\w$]+', header, re.I):
            raise UnsupportedSQL('TABLE_IDENTIFIER', '复杂表名或建表头无法安全解析')
        tbl = re.sub(r'^CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?', '', header, flags=re.I).strip()
        if ctx.direction == 'mysql_to_opengauss':
            return self._mysql(sql[first+1:last], header, tbl, tail, ctx)
        return self._opengauss(sql[first+1:last], header, tbl, tail, ctx)

    def _mysql(self, body: str, header: str, tbl: str, tail: str, ctx: Context) -> str:
        indexes = []
        comments = []
        cols = []
        for elem in split_top_level(body):
            if not elem:
                continue
            if re.match(r'^(?:FULLTEXT|SPATIAL)\s+', elem, re.I):
                raise UnsupportedSQL('SPECIAL_INDEX', '全文索引或空间索引需要人工转换')
            mi = re.match(r'^(?:(UNIQUE)\s+)?(?:KEY|INDEX)\s+([`"\w$]+)\s*\((.*)\)\s*(?:USING\s+(BTREE|HASH))?$', elem, re.I | re.S)
            if mi:
                unique, name, fields, using = mi.groups()
                if using and using.upper() != 'BTREE':
                    raise UnsupportedSQL('HASH_INDEX', '非 BTREE 索引需要独立规则')
                column_sql, expression = _convert_mysql_index_columns(fields, ctx)
                if unique and not expression:
                    # A plain-column UNIQUE KEY is a constraint; keep its name.
                    cols.append(f'CONSTRAINT {_identifier(name, ctx)} UNIQUE ({column_sql})')
                else:
                    kind = 'CREATE UNIQUE INDEX' if unique else 'CREATE INDEX'
                    indexes.append(f'{kind} {_identifier(name, ctx)} ON {_identifier(tbl, ctx)} ({column_sql});')
                    if expression:
                        ctx.warning('EXPRESSION_INDEX_REVIEW', '条件表达式唯一索引已保留；请在目标 openGauss 版本和兼容模式下确认语法与唯一性语义')
                continue
            if re.match(r'^FOREIGN\s+KEY\b', elem, re.I) or (re.match(r'^CONSTRAINT\b', elem, re.I) and re.search(r'\bFOREIGN\s+KEY\b', elem, re.I)):
                raise UnsupportedSQL('FOREIGN_KEY_IMPORT_ORDER', '外键表需要按依赖顺序创建/导入后补建外键，不能直接照搬 MySQL 的 FOREIGN_KEY_CHECKS=0')
            if re.match(r'^(?:PRIMARY\s+KEY|UNIQUE(?:\s+KEY)?\s*\(|CONSTRAINT\s+)', elem, re.I):
                if re.search(r'\b(?:USING\s+HASH|DEFERRABLE|MATCH\s+FULL)\b', elem, re.I):
                    raise UnsupportedSQL('COMPLEX_CONSTRAINT', '复杂约束需要额外转换规则')
                elem = re.sub(r'\bUNIQUE\s+KEY\b', 'UNIQUE', elem, flags=re.I)
                elem = re.sub(r'\bPRIMARY\s+KEY\s+USING\s+BTREE\b', 'PRIMARY KEY', elem, flags=re.I)
                # MySQL permits USING BTREE after a PRIMARY KEY declaration,
                # whereas the generated openGauss constraint does not.
                elem = re.sub(r'\s+USING\s+BTREE\s*$', '', elem, flags=re.I)
                cols.append(ctx.quotes(elem))
                continue
            m = re.match(r'^([`"\w$]+)\s+(.+)$', elem, re.S)
            if not m:
                raise UnsupportedSQL('COLUMN_PARSE', f'无法识别字段定义：{elem[:80]}')
            col, attributes = m.groups()
            col_comment = re.search(r"\bCOMMENT\s+('(?:''|[^'])*')\s*$", attributes, re.I | re.S)
            if col_comment:
                comments.append(f'COMMENT ON COLUMN {_identifier(tbl, ctx)}.{_identifier(col, ctx)} IS {col_comment.group(1)};')
                attributes = attributes[:col_comment.start()].rstrip()
            attributes = _strip_mysql_column_collation(attributes, ctx)
            attributes, rule = replace_type(attributes, ctx.direction, ctx.mappings)
            _report_mapping_usage(ctx, rule)
            if DANGEROUS_COLUMN_ATTRIBUTES.search(_masked_sql_literals(attributes)):
                raise UnsupportedSQL('COLUMN_ATTRIBUTE', '列含有特殊属性（字符集、ON UPDATE、生成列等），需要额外规则')
            if re.search(r'\bUNSIGNED\b', attributes, re.I):
                raise UnsupportedSQL('UNSIGNED_RANGE', '存在未处理的 UNSIGNED 类型，可能产生数值溢出')
            if re.search(r'\bAUTO_INCREMENT\b', attributes, re.I):
                if re.search(r'\bDEFAULT\b', attributes, re.I):
                    raise UnsupportedSQL('IDENTITY_DEFAULT', 'AUTO_INCREMENT 与 DEFAULT 同时出现，请人工确认')
                attributes = re.sub(r'\bAUTO_INCREMENT\b', 'GENERATED BY DEFAULT AS IDENTITY', attributes, flags=re.I)
                ctx.warning('IDENTITY', 'AUTO_INCREMENT 改为 IDENTITY；批量插入显式 ID 后必须检查并同步自增序列起点')
            if not rule and not SAFE_GENERIC_TYPES.match(attributes):
                raise UnsupportedSQL('UNMAPPED_TYPE', f'没有可用的目标字段类型映射：{attributes[:70]}')
            cols.append(f'{_identifier(col, ctx)} {ctx.quotes(attributes)}')
        table_comment = re.search(r"\bCOMMENT\s*=\s*('(?:''|[^'])*')", tail, re.I)
        if table_comment:
            comments.append(f'COMMENT ON TABLE {_identifier(tbl, ctx)} IS {table_comment.group(1)};')
            tail = tail[:table_comment.start()] + tail[table_comment.end():]
        # Ignore only *known* MySQL-only table options. Unknown options block conversion.
        ignored = [
            r'\bENGINE\s*=\s*\w+', r'\bDEFAULT\s+CHARSET\s*=\s*[\w-]+',
            r'\bCHARSET\s*=\s*[\w-]+', r'\b(?:DEFAULT\s+)?CHARACTER\s+SET\s*=\s*[\w-]+',
            r'\bCOLLATE\s*=\s*[\w-]+', r'\bROW_FORMAT\s*=\s*\w+',
            r'\bAUTO_INCREMENT\s*=\s*\d+', r'\bPACK_KEYS\s*=\s*\w+',
        ]
        initial_tail = tail
        for pat in ignored:
            tail = re.sub(pat, '', tail, flags=re.I)
        if tail.strip():
            raise UnsupportedSQL('UNKNOWN_TABLE_OPTION', f'未支持的建表选项：{tail.strip()[:90]}')
        if initial_tail.strip():
            ctx.info('TABLE_OPTIONS', '已移除 MySQL 专有的存储引擎/表选项；字符序兼容性需按目标库验证')
        result = ctx.quotes(header) + ' (\n    ' + ',\n    '.join(cols) + '\n);'
        return '\n'.join([result, *indexes, *comments])

    def _opengauss(self, body: str, header: str, tbl: str, tail: str, ctx: Context) -> str:
        if tail.strip():
            raise UnsupportedSQL('OG_TABLE_OPTIONS', 'openGauss 建表尾部选项需要人工确认')
        cols = []
        for elem in split_top_level(body):
            if not elem:
                continue
            if re.match(r'^(?:CONSTRAINT|PRIMARY\s+KEY|UNIQUE\b|FOREIGN\s+KEY|CHECK\s*\()', elem, re.I):
                if re.search(r'\b(?:DEFERRABLE|EXCLUDE|USING\s+INDEX)\b', elem, re.I):
                    raise UnsupportedSQL('COMPLEX_CONSTRAINT', '复杂约束需要专用处理')
                cols.append(ctx.quotes(elem))
                continue
            m = re.match(r'^([`"\w$]+)\s+(.+)$', elem, re.S)
            if not m:
                raise UnsupportedSQL('COLUMN_PARSE', f'无法解析字段定义：{elem[:80]}')
            col, attr = m.groups()
            if re.search(r'\b(?:TIMESTAMP|TIME)\s+WITH\s+TIME\s+ZONE\b', attr, re.I):
                raise UnsupportedSQL('TIMEZONE_TYPE', '带时区类型到 MySQL 需要业务规则')
            if re.search(r'\b(?:ARRAY|COLLATE|GENERATED\s+ALWAYS\s+AS\s*\(|REFERENCES)\b|::', attr, re.I):
                raise UnsupportedSQL('COLUMN_ATTRIBUTE', '列包含特殊类型或表达式')
            if re.search(r'\b(?:SMALLSERIAL|BIGSERIAL|SERIAL)\b', attr, re.I):
                attr = re.sub(r'\bSMALLSERIAL\b', 'SMALLINT AUTO_INCREMENT', attr, flags=re.I)
                attr = re.sub(r'\bBIGSERIAL\b', 'BIGINT AUTO_INCREMENT', attr, flags=re.I)
                attr = re.sub(r'\bSERIAL\b', 'INT AUTO_INCREMENT', attr, flags=re.I)
            elif re.search(r'\bGENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY\b', attr, re.I):
                attr = re.sub(r'\bGENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY(?:\s*\([^)]*\))?', 'AUTO_INCREMENT', attr, flags=re.I)
                ctx.warning('IDENTITY', '生成列的自增模式已转换；需检查 MySQL 主键条件')
            attr, rule = replace_type(attr, ctx.direction, ctx.mappings)
            _report_mapping_usage(ctx, rule)
            if not rule and not SAFE_GENERIC_TYPES.match(attr):
                raise UnsupportedSQL('UNMAPPED_TYPE', f'目标类型未支持：{attr[:70]}')
            cols.append(f'{_identifier(col, ctx)} {ctx.quotes(attr)}')
        return ctx.quotes(header) + ' (\n    ' + ',\n    '.join(cols) + '\n);'


class CreateIndexRule(Rule):
    def accepts(self, sql: str) -> bool:
        return bool(re.match(r'^CREATE\s+(?:UNIQUE\s+)?INDEX\s+', sql, re.I))

    def convert(self, sql: str, ctx: Context) -> str:
        if re.search(r'\b(?:CONCURRENTLY|USING\s+(?:GIN|GIST|HASH)|INCLUDE|WHERE)\b', sql, re.I):
            raise UnsupportedSQL('INDEX_OPTIONS', '特殊索引类型或部分索引需要单独规则')
        m = re.fullmatch(r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+[`"\w$]+\s+ON\s+[`"\w$.]+\s*\((.+)\)', _no_semicolon(sql), re.I | re.S)
        if not m:
            raise UnsupportedSQL('INDEX_FORM', '只支持普通列名组成的 CREATE INDEX')
        for part in split_top_level(m.group(1)):
            if not re.fullmatch(r'[`"\w$]+(?:\s+(?:ASC|DESC))?', part, re.I):
                raise UnsupportedSQL('INDEX_EXPRESSION', '表达式索引、前缀索引或排序规则需要专用规则')
        return ctx.quotes(_no_semicolon(sql)) + ';'


class DropTableRule(Rule):
    def accepts(self, sql: str) -> bool:
        return bool(re.match(r'^DROP\s+TABLE\s+', sql, re.I))

    def convert(self, sql: str, ctx: Context) -> str:
        if not re.fullmatch(r'DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?[`"\w$]+(?:\.[`"\w$]+)?(?:\s+CASCADE|\s+RESTRICT)?\s*;?', sql, re.I):
            raise UnsupportedSQL('DROP_TABLE_OPTIONS', '复杂删除表语法需要人工确认')
        if re.search(r'\bCASCADE\b', sql, re.I) and ctx.direction == 'opengauss_to_mysql':
            raise UnsupportedSQL('DROP_CASCADE', 'MySQL 不支持同等语义的 CASCADE 删除表')
        return ctx.quotes(_no_semicolon(sql)) + ';'


class AlterTableRule(Rule):
    """A safe subset of ALTER TABLE (schema evolution, not proprietary DDL)."""
    def accepts(self, sql: str) -> bool:
        return bool(re.match(r'^ALTER\s+TABLE\s+', sql, re.I))

    def convert(self, sql: str, ctx: Context) -> str:
        m = re.match(r'^ALTER\s+TABLE\s+(?:ONLY\s+)?([`"\w$.]+)\s+(.+)$', _no_semicolon(sql), re.I | re.S)
        if not m:
            raise UnsupportedSQL('ALTER_TABLE_PARSE', '复杂 ALTER TABLE 头部无法解析')
        table, operations = m.groups()
        tbl = _identifier(table, ctx)
        results = []
        for op in split_top_level(operations):
            op = op.strip()
            if not op:
                continue
            if re.fullmatch(r'DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?[`"\w$]+', op, re.I):
                results.append(f'ALTER TABLE {tbl} {ctx.quotes(op)};')
                continue
            if re.fullmatch(r'ADD\s+(?:CONSTRAINT\s+[`"\w$]+\s+)?(?:PRIMARY\s+KEY|UNIQUE|CHECK)\s*\([^;]+\)', op, re.I | re.S):
                results.append(f'ALTER TABLE {tbl} {ctx.quotes(op)};')
                continue
            if ctx.direction == 'mysql_to_opengauss':
                unique = re.fullmatch(r'ADD\s+UNIQUE\s+(?:KEY|INDEX)\s+([`"\w$]+)\s*\((.+)\)', op, re.I | re.S)
                if unique:
                    name, columns = unique.groups()
                    if re.search(r'[`"\w$]+\s*\(\s*\d+\s*\)', columns):
                        raise UnsupportedSQL('PREFIX_INDEX', '不能将前缀唯一索引当作完整列唯一约束')
                    results.append(f'ALTER TABLE {tbl} ADD CONSTRAINT {_identifier(name, ctx)} UNIQUE ({ctx.quotes(columns)});')
                    continue
                idx = re.fullmatch(r'ADD\s+(?:KEY|INDEX)\s+([`"\w$]+)\s*\((.+)\)', op, re.I | re.S)
                if idx:
                    name, columns = idx.groups()
                    if re.search(r'[`"\w$]+\s*\(\s*\d+\s*\)', columns):
                        raise UnsupportedSQL('PREFIX_INDEX', 'MySQL 前缀索引需要单独处理')
                    results.append(f'CREATE INDEX {_identifier(name, ctx)} ON {tbl} ({ctx.quotes(columns)});')
                    continue
            add = re.match(r'^ADD\s+(?:COLUMN\s+)?(?!INDEX|KEY|CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK)([`"\w$]+)\s+(.+)$', op, re.I | re.S)
            if add:
                name, attr = add.groups()
                _ensure_safe_expressions(attr, ctx)
                if re.search(r'\b(?:UNSIGNED|COLLATE|CHARACTER\s+SET|ON\s+UPDATE|REFERENCES|ENUM|SET)\b', attr, re.I):
                    # UNSIGNED may be legitimately covered by a type mapping.
                    probe, rule = replace_type(attr, ctx.direction, ctx.mappings)
                    if re.search(r'\b(?:UNSIGNED|COLLATE|CHARACTER\s+SET|ON\s+UPDATE|REFERENCES|ENUM|SET)\b', probe, re.I):
                        raise UnsupportedSQL('ALTER_COLUMN_ATTRIBUTE', '新增列包含特殊类型或属性')
                converted, rule = replace_type(attr, ctx.direction, ctx.mappings)
                _report_mapping_usage(ctx, rule)
                if re.search(r'\bAUTO_INCREMENT\b', converted, re.I):
                    if ctx.direction == 'mysql_to_opengauss':
                        converted = re.sub(r'\bAUTO_INCREMENT\b', 'GENERATED BY DEFAULT AS IDENTITY', converted, flags=re.I)
                    else:
                        raise UnsupportedSQL('ALTER_IDENTITY', '自增属性需人工确认')
                if not rule and not SAFE_GENERIC_TYPES.match(converted):
                    raise UnsupportedSQL('UNMAPPED_TYPE', '新增列的数据类型未支持')
                results.append(f'ALTER TABLE {tbl} ADD COLUMN {_identifier(name, ctx)} {ctx.quotes(converted)};')
                continue
            raise UnsupportedSQL('ALTER_TABLE_OPTION', f'未支持的 ALTER TABLE 子操作：{op[:100]}')
        if not results:
            raise UnsupportedSQL('ALTER_TABLE_EMPTY', 'ALTER TABLE 没有可转换操作')
        return '\n'.join(results)


class DmlRule(Rule):
    def accepts(self, sql: str) -> bool:
        return bool(re.match(r'^(?:INSERT|UPDATE|DELETE)\s+', sql, re.I))

    def convert(self, sql: str, ctx: Context) -> str:
        if SENSITIVE_SQL.search(sql):
            raise UnsupportedSQL('DIALECT_DML', '存在 ON CONFLICT、INSERT IGNORE、RETURNING 等方言特性')
        _ensure_safe_expressions(sql, ctx)
        if re.match(r'^INSERT\b', sql, re.I):
            if not re.match(r'^INSERT\s+INTO\s+[`"\w$.]+\s*(?:\([^;]*?\)\s*)?VALUES\s*\(', sql, re.I | re.S):
                raise UnsupportedSQL('INSERT_FORM', '仅支持 INSERT INTO ... VALUES (...) 形式')
        if re.match(r'^UPDATE\b', sql, re.I):
            if not re.match(r'^UPDATE\s+[`"\w$.]+\s+SET\s+', sql, re.I):
                raise UnsupportedSQL('UPDATE_FORM', '复杂 UPDATE / JOIN 更新需要专用转换规则')
        if re.match(r'^DELETE\b', sql, re.I):
            if not re.match(r'^DELETE\s+FROM\s+[`"\w$.]+(?:\s+WHERE\b|\s*$|\s*;)', sql, re.I):
                raise UnsupportedSQL('DELETE_FORM', '复杂 DELETE / JOIN 删除需要专用转换规则')
        return ctx.quotes(_no_semicolon(sql)) + ';'


# Adding an implementation to this explicit registry extends supported syntax.
# No untrusted plugins are dynamically loaded from user SQL/files.
RULES: tuple[Rule, ...] = (
    SkipSessionRule(), TransactionRule(), CreateTableRule(), CreateIndexRule(),
    DropTableRule(), AlterTableRule(), DmlRule()
)


def _process_statement(stmt: Statement, direction: str, mappings: dict[str, str], report: Report) -> str | None:
    """Run one statement through the same rules for text and file conversion."""
    if stmt.too_large:
        report.total += 1
        report.blocked += 1
        report.add('BLOCKER', 'STATEMENT_TOO_LARGE', stmt.line,
                   f'单条 SQL 超过 {MAX_STATEMENT_BYTES // 1024**2} MiB 内存安全阈值；请将扩展 INSERT 分批导出')
        return f'-- [BLOCKER] source line {stmt.line}: STATEMENT_TOO_LARGE\n-- See JSON report.'
    source = strip_leading_comments(stmt.sql)
    if not source:
        return None
    report.total += 1
    relative = stmt.sql.find(source)
    source_line = stmt.line + stmt.sql[:relative].count('\n') if relative >= 0 else stmt.line
    ctx = Context(direction, mappings, report, source_line)
    try:
        if direction == 'mysql_to_opengauss':
            # Rewrite only SQL string tokens, not comments/identifiers. This
            # preserves JSON, exception stack traces, and log payload bytes.
            source, _ = normalize_mysql_literals(source)
        if stmt.unterminated:
            raise UnsupportedSQL('UNTERMINATED_SQL', 'SQL 文件结束时存在未闭合的字符串、注释或 dollar quote')
        if source.startswith('/*!') and re.match(r'^/\*!\d+\s+SET\b', source, re.I):
            result = ''
            ctx.info('DUMP_SESSION', '跳过源数据库的版本化会话设置')
        elif re.match(r'^(?:DELIMITER|\\\.|COPY\b)', source, re.I):
            raise UnsupportedSQL('DUMP_DIRECTIVE', '导出脚本包含客户端专有命令或 COPY 数据块')
        else:
            rule = next((rule for rule in RULES if rule.accepts(source)), None)
            if rule is None:
                raise UnsupportedSQL('UNSUPPORTED_STATEMENT', f'未支持的 SQL 语句：{source[:80].strip()}')
            result = rule.convert(source, ctx)
        if result:
            report.converted += 1
            return result
        report.skipped += 1
        return None
    except (UnsupportedSQL, ValueError) as exc:
        report.blocked += 1
        code = exc.code if isinstance(exc, UnsupportedSQL) else ('STRING_LITERAL' if isinstance(exc, LiteralConversionError) else 'MAPPING_ERROR')
        report.add('BLOCKER', code, source_line, str(exc), source[:350])
        return f'-- [BLOCKER] source line {source_line}: {code}\n-- See JSON report for original SQL.'


def convert_text(sql: str, direction: str, mappings: dict[str, str] | None = None,
                 filename: str = '', progress: Callable[[int, int], None] | None = None) -> tuple[str, Report]:
    if direction not in DIRECTIONS:
        raise ValueError('不支持的转换方向')
    src, dst = ('mysql', 'opengauss') if direction == 'mysql_to_opengauss' else ('opengauss', 'mysql')
    report = Report(src, dst, filename)
    output = [f'-- SQLConverter: {src} -> {dst}\n-- STATIC CONVERSION ONLY: 请在对应版本的测试数据库执行并核对字符序与约束，再用于正式环境。\n']
    if re.search(r'(?mi)^\s*SET\s+[^;\n]*SQL_MODE[^;\n]*NO_BACKSLASH_ESCAPES', sql):
        report.total = report.blocked = 1
        report.add('BLOCKER', 'MYSQL_SQL_MODE', 1, '发现 NO_BACKSLASH_ESCAPES；当前 MySQL 源模式的字符串转义规则不同，已拒绝转换')
        return '-- UNSUPPORTED MYSQL SQL_MODE: no SQL emitted. See report.\n', report
    if re.search(r'(?mi)^\s*DELIMITER\s+\S+', sql):
        report.total = report.blocked = 1
        report.add('BLOCKER', 'DELIMITER_MODE', 1, '包含 DELIMITER 客户端指令；为防止拆坏过程体，已终止整个文件的转换')
        return '-- UNSAFE DUMP MODE: no SQL statements emitted. See report.\n', report
    if re.search(r'(?mi)^\s*COPY\b[^\n]*\bFROM\s+STDIN\s*;', sql):
        report.total = report.blocked = 1
        report.add('BLOCKER', 'COPY_STDIN', 1, '包含 COPY FROM STDIN 数据块；请重新导出为 INSERT 格式')
        return '-- COPY STDIN DUMP: no SQL statements emitted. See report.\n', report
    statements = split_sql(sql)
    count = len(statements)
    for n, stmt in enumerate(statements, start=1):
        result = _process_statement(stmt, direction, mappings or {}, report)
        if result:
            output.append(result)
        if progress and (n % 20 == 0 or n == count):
            progress(n, count)
    if report.blocked:
        output.insert(0, '-- MANUAL REVIEW REQUIRED: unsupported statements were omitted.\n-- DO NOT execute as a complete migration script.\n')
    return '\n\n'.join(output).rstrip() + '\n', report


def read_sql(path: Path, encoding: str = 'auto', max_bytes: int = MAX_SQL_FILE_BYTES) -> str:
    validate_file_size(path, max_bytes)
    raw = path.read_bytes()
    candidates = ['utf-8-sig', 'gb18030'] if encoding == 'auto' else [encoding]
    for codec in candidates:
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            pass
    raise ValueError('无法识别 SQL 文件编码，请手动选择正确编码')


def default_output_name(path: Path, direction: str) -> str:
    target = 'opengauss' if direction == 'mysql_to_opengauss' else 'mysql'
    stem = re.sub(r'(?i)_(?:mysql|opengauss)(?:_review)?$', '', path.stem)
    return stem + '_' + target + '.sql'


def validate_output_name(value: str) -> str:
    name = value.strip()
    if not name or not name.lower().endswith('.sql') or name.lower() == '.sql':
        raise ValueError('输出文件名必须是非空的 .sql 文件名')
    if name in ('.', '..') or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((' ', '.')):
        raise ValueError('输出文件名含有 Windows 不允许的字符或路径')
    if len(name) > 180 or Path(name).name != name:
        raise ValueError('输出文件名过长或包含路径')
    return name


def convert_file(path: Path, output_dir: Path, direction: str,
                 mappings: dict[str, str] | None = None, encoding: str = 'auto',
                 progress: Callable[[int, int], None] | None = None,
                 output_name: str | None = None,
                 cancel_check: Callable[[], bool] | None = None) -> tuple[Path, Path, Report]:
    """Convert a <=500 MiB SQL dump without reading it all into RAM.

    Output is written into a temporary file first and published only after
    conversion/report generation finishes. A failed conversion leaves no
    partial result under the final filename.
    """
    if direction not in DIRECTIONS:
        raise ValueError('不支持的转换方向')
    started_at = time.perf_counter()
    path = Path(path)
    size = validate_file_size(path)
    if cancel_check and cancel_check():
        raise ConversionCancelled('用户已取消转换')
    original_stat = path.stat()
    output_filename = validate_output_name(output_name if output_name is not None else
                                           default_output_name(path, direction))
    if progress:
        progress(1, 100)
    def check_cancel() -> None:
        if cancel_check and cancel_check():
            raise ConversionCancelled('用户已取消转换')
    codec, directive = preflight_sql(path, encoding, on_chunk=check_cancel)
    if cancel_check and cancel_check():
        raise ConversionCancelled('用户已取消转换')
    if progress:
        progress(3, 100)
    src, dst = ('mysql', 'opengauss') if direction == 'mysql_to_opengauss' else ('opengauss', 'mysql')
    report = Report(src, dst, path.name)
    if progress:
        progress(5, 100)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8-sig', newline='\n',
                                         prefix='.sqlconverter-', suffix='.tmp',
                                         dir=output_dir, delete=False) as output:
            temp_name = Path(output.name)
            if directive:
                code, message = directive
                report.total = report.blocked = 1
                report.add('BLOCKER', code, 1, message)
                output.write(f'-- UNSAFE DUMP MODE: {code}; no SQL statements emitted. See report.\n')
            else:
                output.write(f'-- SQLConverter: {src} -> {dst}\n')
                output.write('-- STATIC CONVERSION ONLY: 请在对应版本的测试数据库执行并核对字符序与约束，再用于正式环境。\n\n')
                with path.open('r', encoding=codec, errors='strict', newline='') as source:
                    # Progress reports consumed characters against source bytes.
                    # Accurate for ASCII exports; estimated for multibyte SQL.
                    last_percent = -1
                    def update_chars(chars: int) -> None:
                        nonlocal last_percent
                        if cancel_check and cancel_check():
                            raise ConversionCancelled('用户已取消转换')
                        if progress:
                            # Emit at most ~95 GUI updates, not one Qt signal per
                            # 256 KiB input chunk (nearly 2,000 for 500 MiB).
                            percent = 5 + (94 * min(chars, size)) // size
                            if percent > last_percent:
                                last_percent = percent
                                progress(percent, 100)
                    for stmt in iter_sql_stream(source, on_chars=update_chars):
                        if cancel_check and cancel_check():
                            raise ConversionCancelled('用户已取消转换')
                        result = _process_statement(stmt, direction, mappings or {}, report)
                        if result:
                            output.write(result)
                            output.write('\n\n')
                if cancel_check and cancel_check():
                    raise ConversionCancelled('用户已取消转换')
                end_stat = path.stat()
                if end_stat.st_size != size or end_stat.st_mtime_ns != original_stat.st_mtime_ns:
                    raise ValueError('转换期间源 SQL 文件发生变化，请确认没有其他程序修改文件后重新执行')
                if progress:
                    progress(99, 100)
        if cancel_check and cancel_check():
            raise ConversionCancelled('用户已取消转换')
        if report.blocked and not directive:
            # Prepend the review marker with bounded memory. Cancellation must
            # remove *both* temp files, including the in-progress second copy.
            review_name = None
            try:
                with tempfile.NamedTemporaryFile('wb', prefix='.sqlconverter-review-', suffix='.tmp',
                                                 dir=output_dir, delete=False) as reviewed:
                    review_name = Path(reviewed.name)
                    reviewed.write(codecs.BOM_UTF8)
                    reviewed.write(b'-- MANUAL REVIEW REQUIRED: unsupported statements were omitted.\n'
                                   b'-- DO NOT execute as a complete migration script.\n\n')
                    with temp_name.open('rb') as original:
                        if original.read(3) != codecs.BOM_UTF8:
                            original.seek(0)
                        while chunk := original.read(1024 * 1024):
                            if cancel_check and cancel_check():
                                raise ConversionCancelled('用户已取消转换')
                            reviewed.write(chunk)
                temp_name.unlink()
                temp_name = review_name
                review_name = None
            finally:
                if review_name is not None:
                    review_name.unlink(missing_ok=True)

        base = output_dir / Path(output_filename).stem
        suffix = '_review' if not report.ready and not base.name.lower().endswith('_review') else ''
        candidate = base
        serial = 2
        while (candidate.with_name(candidate.name + suffix + '.sql').exists() or
               candidate.with_name(candidate.name + '_report.json').exists()):
            candidate = base.with_name(base.name + '_' + str(serial))
            serial += 1
        sql_path = candidate.with_name(candidate.name + suffix + '.sql')
        report_path = candidate.with_name(candidate.name + '_report.json')
        # Runtime information helps users estimate performance on their own
        # Windows PCs, without making an unsupported universal ETA promise.
        elapsed = max(time.perf_counter() - started_at, 0.001)
        report.metrics = {
            'input_bytes': size,
            'elapsed_seconds': round(elapsed, 3),
            'average_mib_per_second': round(size / (1024 * 1024 * elapsed), 3),
        }
        # Report serialization is bounded; conversion never stores entire SQL.
        report_tmp = output_dir / (temp_name.name + '.json')
        try:
            report_tmp.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
            # Both individual file replaces are atomic on the same filesystem.
            os.replace(temp_name, sql_path)
            temp_name = None
            os.replace(report_tmp, report_path)
        finally:
            report_tmp.unlink(missing_ok=True)
        if progress:
            progress(100, 100)
        return sql_path, report_path, report
    finally:
        if temp_name is not None:
            temp_name.unlink(missing_ok=True)
