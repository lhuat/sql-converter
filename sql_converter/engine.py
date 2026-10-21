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

from .mappings import replace_type
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

    @property
    def ready(self) -> bool:
        return self.blocked == 0

    def add(self, severity: str, code: str, line: int, message: str, excerpt: str = '') -> None:
        self.findings.append(Finding(severity, code, line, message, excerpt[:350]))

    def to_dict(self) -> dict:
        output = asdict(self)
        output['status'] = 'READY_FOR_TESTING' if self.ready else 'MANUAL_REVIEW_REQUIRED'
        return output

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


def _ensure_safe_expressions(sql: str, ctx: Context) -> None:
    if has_unsafe_string_escapes(sql, ctx.direction):
        raise UnsupportedSQL('STRING_ESCAPE', '字符串含有方言相关的反斜线转义，无法保证数据值不变')
    if re.search(r'\b(?:0x[0-9a-f]+|B\s*\'[^\']+\'|X\s*\'[^\']+\')', sql, re.I):
        raise UnsupportedSQL('BINARY_LITERAL', '二进制或十六进制字面量需要专用转换规则')
    if ctx.direction == 'mysql_to_opengauss' and re.search(r'"(?:[^"]|"")*"', sql):
        raise UnsupportedSQL('MYSQL_DOUBLE_QUOTE', 'MySQL 双引号可能表示字符串（取决于 ANSI_QUOTES），不能直接迁移')
    if re.search(r'\b(?:NOW\(|IFNULL\(|DATE_FORMAT\(|TO_CHAR\(|NVL\(|IF\()', sql, re.I):
        raise UnsupportedSQL('DIALECT_FUNCTION', '包含数据库专有函数，请增加对应语法规则')
    if re.search(r'::\s*[A-Za-z_]', sql):
        raise UnsupportedSQL('PG_CAST', 'PostgreSQL 风格类型强制转换需要专用规则')
    if re.search(r'\$[A-Za-z_0-9]*\$', sql):
        raise UnsupportedSQL('DOLLAR_QUOTE', '包含 dollar-quoted SQL 代码块')


def _identifier(raw: str, ctx: Context) -> str:
    return ctx.quotes(raw.strip())


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
            ctx.warning('SESSION_SETTING', '跳过会话级设置；目标数据库需要独立确认字符集和约束设置')
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
                if re.search(r'[`"\w$]+\s*\(\s*\d+\s*\)', fields):
                    raise UnsupportedSQL('PREFIX_INDEX', 'MySQL 前缀索引无法简单改写为普通索引')
                if using and using.upper() != 'BTREE':
                    raise UnsupportedSQL('HASH_INDEX', '非 BTREE 索引需要独立规则')
                if unique:
                    cols.append(f'CONSTRAINT {_identifier(name, ctx)} UNIQUE ({ctx.quotes(fields)})')
                else:
                    indexes.append(f'CREATE INDEX {_identifier(name, ctx)} ON {_identifier(tbl, ctx)} ({ctx.quotes(fields)});')
                continue
            if re.match(r'^(?:PRIMARY\s+KEY|UNIQUE(?:\s+KEY)?\s*\(|FOREIGN\s+KEY|CONSTRAINT\s+)', elem, re.I):
                if re.search(r'\b(?:USING\s+HASH|DEFERRABLE|MATCH\s+FULL)\b', elem, re.I):
                    raise UnsupportedSQL('COMPLEX_CONSTRAINT', '复杂约束需要额外转换规则')
                elem = re.sub(r'\bUNIQUE\s+KEY\b', 'UNIQUE', elem, flags=re.I)
                elem = re.sub(r'\bPRIMARY\s+KEY\s+USING\s+BTREE\b', 'PRIMARY KEY', elem, flags=re.I)
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
            attributes, rule = replace_type(attributes, ctx.direction, ctx.mappings)
            if DANGEROUS_COLUMN_ATTRIBUTES.search(attributes):
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
            r'\bCHARSET\s*=\s*[\w-]+', r'\bDEFAULT\s+CHARACTER\s+SET\s*=\s*[\w-]+',
            r'\bCOLLATE\s*=\s*[\w-]+', r'\bROW_FORMAT\s*=\s*\w+',
            r'\bAUTO_INCREMENT\s*=\s*\d+', r'\bPACK_KEYS\s*=\s*\w+',
        ]
        initial_tail = tail
        for pat in ignored:
            tail = re.sub(pat, '', tail, flags=re.I)
        if tail.strip():
            raise UnsupportedSQL('UNKNOWN_TABLE_OPTION', f'未支持的建表选项：{tail.strip()[:90]}')
        if initial_tail.strip():
            ctx.warning('TABLE_OPTIONS', '去除引擎、字符集等 MySQL 表选项，请检查目标数据库配置')
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


def convert_text(sql: str, direction: str, mappings: dict[str, str] | None = None,
                 filename: str = '', progress: Callable[[int, int], None] | None = None) -> tuple[str, Report]:
    if direction not in DIRECTIONS:
        raise ValueError('不支持的转换方向')
    src, dst = ('mysql', 'opengauss') if direction == 'mysql_to_opengauss' else ('opengauss', 'mysql')
    report = Report(src, dst, filename)
    output = [f'-- SQLConverter: {src} -> {dst}\n-- WARNING: 在测试数据库执行并检查报告后，再用于正式环境。\n']
    # These are client-side dump modes, not ordinary semicolon-delimited SQL.
    # Splitting them as statements would risk executing fragments of a routine
    # or treating COPY data as executable SQL: block the complete file early.
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
        source = strip_leading_comments(stmt.sql)
        if not source:
            continue
        report.total += 1
        relative = stmt.sql.find(source)
        source_line = stmt.line + stmt.sql[:relative].count('\n') if relative >= 0 else stmt.line
        ctx = Context(direction, mappings or {}, report, source_line)
        try:
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
                output.append(result)
                report.converted += 1
            else:
                report.skipped += 1
        except (UnsupportedSQL, ValueError) as exc:
            report.blocked += 1
            code = exc.code if isinstance(exc, UnsupportedSQL) else 'MAPPING_ERROR'
            report.add('BLOCKER', code, source_line, str(exc), source[:350])
            # No unverified SQL is emitted as an executable statement.
            output.append(f'-- [BLOCKER] source line {source_line}: {code}\n-- See JSON report for original SQL.')
        if progress and (n % 20 == 0 or n == count):
            progress(n, count)
    if report.blocked:
        output.insert(0, '-- MANUAL REVIEW REQUIRED: unsupported statements were omitted.\n-- DO NOT execute as a complete migration script.\n')
    if mappings:
        for key, value in mappings.items():
            report.add('WARNING', 'CUSTOM_MAPPING', 1, f'自定义类型映射 {key} → {value}；请确认目标类型兼容性')
    return '\n\n'.join(output).rstrip() + '\n', report


def read_sql(path: Path, encoding: str = 'auto', max_bytes: int = 80 * 1024 * 1024) -> str:
    if path.stat().st_size > max_bytes:
        raise ValueError('文件超过 80 MiB 安全上限。请分批导出后转换。')
    raw = path.read_bytes()
    candidates = ['utf-8-sig', 'gb18030'] if encoding == 'auto' else [encoding]
    for codec in candidates:
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            pass
    raise ValueError('无法识别 SQL 文件编码，请手动选择正确编码')


def convert_file(path: Path, output_dir: Path, direction: str,
                 mappings: dict[str, str] | None = None, encoding: str = 'auto',
                 progress: Callable[[int, int], None] | None = None) -> tuple[Path, Path, Report]:
    text = read_sql(path, encoding)
    result, report = convert_text(text, direction, mappings, path.name, progress)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = 'opengauss' if direction == 'mysql_to_opengauss' else 'mysql'
    stem = re.sub(r'(?i)_(?:mysql|opengauss)(?:_review)?$', '', path.stem)
    base = output_dir / (stem + '_' + target)
    # Do not overwrite a previous conversion result or report.
    suffix = '_review' if not report.ready else ''
    candidate = base
    serial = 2
    while (candidate.with_name(candidate.name + suffix + '.sql').exists() or
           candidate.with_name(candidate.name + '_report.json').exists()):
        candidate = base.with_name(base.name + '_' + str(serial))
        serial += 1
    sql_path = candidate.with_name(candidate.name + suffix + '.sql')
    report_path = candidate.with_name(candidate.name + '_report.json')
    sql_path.write_text(result, encoding='utf-8-sig')
    report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
    return sql_path, report_path, report
