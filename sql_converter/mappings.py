"""Versioned, user-overridable SQL type mappings.

The mapping keys are *type templates*, rather than actual column types. A
mapping such as VARCHAR(n) therefore applies to VARCHAR(20) and VARCHAR(255).
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULTS = {
    "mysql_to_opengauss": [
        ("TINYINT(1)", "SMALLINT", ["SMALLINT", "BOOLEAN", "INTEGER"], "1 不一定表示布尔值"),
        ("TINYINT(n) UNSIGNED", "SMALLINT", ["SMALLINT", "INTEGER"], "保留无符号正整数"),
        ("TINYINT(n)", "SMALLINT", ["SMALLINT", "INTEGER"], "忽略旧版整数显示宽度"),
        ("TINYINT", "SMALLINT", ["SMALLINT", "INTEGER"], "保留整数语义"),
        ("BIGINT(n) UNSIGNED", "NUMERIC(20,0)", ["NUMERIC(20,0)", "DECIMAL(20,0)"], "无符号 BIGINT"),
        ("INT(n) UNSIGNED", "BIGINT", ["BIGINT", "NUMERIC(10,0)"], "无符号整型"),
        ("SMALLINT(n) UNSIGNED", "INTEGER", ["INTEGER", "NUMERIC(5,0)"], "无符号小整型"),
        ("BIGINT(n)", "BIGINT", ["BIGINT"], "忽略整数显示宽度"),
        ("INT(n)", "INTEGER", ["INTEGER"], "忽略整数显示宽度"),
        ("SMALLINT(n)", "SMALLINT", ["SMALLINT"], "忽略整数显示宽度"),
        ("SMALLINT UNSIGNED", "INTEGER", ["INTEGER", "NUMERIC(5,0)"], "防止范围溢出"),
        ("INT UNSIGNED", "BIGINT", ["BIGINT", "NUMERIC(10,0)"], "防止范围溢出"),
        ("BIGINT UNSIGNED", "NUMERIC(20,0)", ["NUMERIC(20,0)", "DECIMAL(20,0)"], "超出有符号 BIGINT 范围"),
        ("MEDIUMINT", "INTEGER", ["INTEGER", "BIGINT"], "范围兼容"),
        ("INT", "INTEGER", ["INTEGER", "BIGINT"], "整型"),
        ("INTEGER", "INTEGER", ["INTEGER", "BIGINT"], "整型"),
        ("DATETIME(p)", "TIMESTAMP(p)", ["TIMESTAMP(p)", "TIMESTAMP"], "无时区时间"),
        ("DATETIME", "TIMESTAMP", ["TIMESTAMP", "TIMESTAMP(6)"], "无时区时间"),
        ("LONGTEXT", "TEXT", ["TEXT"], "检查存储和性能"),
        ("MEDIUMTEXT", "TEXT", ["TEXT"], "长文本"),
        ("TINYTEXT", "TEXT", ["TEXT", "VARCHAR(255)"], "文本"),
        ("JSON", "JSON", ["JSON", "JSONB", "TEXT"], "JSONB 行为可能不同"),
        ("LONGBLOB", "BYTEA", ["BYTEA"], "数据字面量需单独转换"),
        ("MEDIUMBLOB", "BYTEA", ["BYTEA"], "数据字面量需单独转换"),
        ("BLOB", "BYTEA", ["BYTEA"], "数据字面量需单独转换"),
        ("VARBINARY(n)", "BYTEA", ["BYTEA"], "二进制字段"),
        ("BINARY(n)", "BYTEA", ["BYTEA"], "二进制字段"),
        ("DOUBLE", "DOUBLE PRECISION", ["DOUBLE PRECISION", "NUMERIC"], "浮点语义"),
        ("FLOAT", "REAL", ["REAL", "DOUBLE PRECISION"], "浮点语义"),
        ("VARCHAR(n)", "VARCHAR(n)", ["VARCHAR(n)", "TEXT"], "保留长度 n"),
        ("CHAR(n)", "CHAR(n)", ["CHAR(n)", "VARCHAR(n)"], "保留长度 n"),
        ("DECIMAL(p,s)", "NUMERIC(p,s)", ["NUMERIC(p,s)", "DECIMAL(p,s)"], "保留精度"),
        ("NUMERIC(p,s)", "NUMERIC(p,s)", ["NUMERIC(p,s)"], "保留精度"),
    ],
    "opengauss_to_mysql": [
        ("BOOLEAN", "TINYINT(1)", ["TINYINT(1)", "BOOLEAN"], "MySQL 布尔值别名"),
        ("SMALLINT", "SMALLINT", ["SMALLINT", "INT"], "整数"),
        ("TIMESTAMP(p)", "DATETIME(p)", ["DATETIME(p)", "TIMESTAMP(p)"], "无时区时使用"),
        ("TIMESTAMP", "DATETIME", ["DATETIME", "TIMESTAMP"], "无时区时使用"),
        ("TEXT", "LONGTEXT", ["LONGTEXT", "TEXT", "MEDIUMTEXT"], "扩大文本上限"),
        ("JSONB", "JSON", ["JSON", "LONGTEXT"], "存储表示可能不同"),
        ("JSON", "JSON", ["JSON", "LONGTEXT"], "JSON"),
        ("BYTEA", "LONGBLOB", ["LONGBLOB", "BLOB"], "二进制字面量需转换"),
        ("DOUBLE PRECISION", "DOUBLE", ["DOUBLE", "DECIMAL(p,s)"], "浮点语义"),
        ("REAL", "FLOAT", ["FLOAT", "DOUBLE"], "浮点语义"),
        ("VARCHAR(n)", "VARCHAR(n)", ["VARCHAR(n)", "TEXT"], "保留长度"),
        ("CHAR(n)", "CHAR(n)", ["CHAR(n)", "VARCHAR(n)"], "保留长度"),
        ("NUMERIC(p,s)", "DECIMAL(p,s)", ["DECIMAL(p,s)", "NUMERIC(p,s)"], "保留精度"),
        ("DECIMAL(p,s)", "DECIMAL(p,s)", ["DECIMAL(p,s)"], "保留精度"),
    ],
}

@dataclass(frozen=True)
class MappingRow:
    source: str
    target: str
    choices: tuple[str, ...]
    note: str


def default_rows(direction: str) -> list[MappingRow]:
    return [MappingRow(a, b, tuple(c), d) for a, b, c, d in DEFAULTS[direction]]


def load_overrides(path: Path) -> dict[str, dict[str, str]]:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(content, dict):
            return {}
        return {d: {str(k): str(v) for k, v in m.items() if isinstance(v, str)}
                for d, m in content.items() if d in DEFAULTS and isinstance(m, dict)}
    except (OSError, ValueError, TypeError):
        return {}


def save_overrides(path: Path, content: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _template_regex(template: str) -> re.Pattern[str]:
    """Match literal type names and templated numeric arguments."""
    out = []
    i = 0
    while i < len(template):
        if template[i] == '(':
            ending = template.find(')', i+1)
            if ending >= 0:
                content = template[i+1:ending]
                if re.fullmatch(r'[nps](?:,[nps])*', content):
                    out.append(r'\s*\(\s*' + r'\s*,\s*'.join([r'(\d+)'] * len(content.split(','))) + r'\s*\)')
                    i = ending + 1
                    continue
        if template[i].isspace():
            while i < len(template) and template[i].isspace():
                i += 1
            out.append(r'\s+')
            continue
        out.append(re.escape(template[i]))
        i += 1
    return re.compile(r'^' + ''.join(out) + r'(?![\w(])', re.I)


TARGET_NAMES = {
    'mysql_to_opengauss': {'SMALLINT','BOOLEAN','INTEGER','INT','BIGINT','NUMERIC','DECIMAL','TIMESTAMP','TEXT','JSON','JSONB','BYTEA','DOUBLE PRECISION','REAL','VARCHAR','CHAR','DATE','TIME','UUID','FLOAT'},
    'opengauss_to_mysql': {'TINYINT','SMALLINT','BOOLEAN','INTEGER','INT','BIGINT','NUMERIC','DECIMAL','DATETIME','TIMESTAMP','LONGTEXT','MEDIUMTEXT','TEXT','JSON','LONGBLOB','BLOB','DOUBLE','FLOAT','VARCHAR','CHAR','DATE','TIME','VARBINARY','BINARY','REAL'},
}


def replace_type(definition: str, direction: str, overrides: dict[str, str]) -> tuple[str, str | None]:
    """Change leading type only, preserving suffix attributes. Returns (SQL, rule)."""
    source = definition.lstrip()
    prefix = definition[:len(definition)-len(source)]
    rows = default_rows(direction)
    # Keep more specific templates ahead of generic types.
    rows.sort(key=lambda row: -len(row.source))
    for row in rows:
        match = _template_regex(row.source).match(source)
        if not match:
            continue
        target = overrides.get(row.source, row.target).strip()
        if not re.fullmatch(r"[\w\s]+(?:\([\w\s,]+\))?", target):
            raise ValueError(f"非法目标类型：{target}")
        for placeholder, value in zip(re.findall(r"[nps]", row.source.split("(")[-1].split(")")[0]) if "(" in row.source else [], match.groups()):
            target = re.sub(rf"\b{placeholder}\b", value, target)
        if re.search(r"\([nps](?:,[nps])*\)", target):
            raise ValueError(f"目标类型 {target} 含有未替换的参数")
        target_name = re.sub(r"\s*\(.*$", "", target).strip().upper()
        if target_name not in TARGET_NAMES[direction]:
            raise ValueError(f"目标数据库不允许的类型映射：{target}")
        src_args = [int(x) for x in match.groups()]
        target_nums_match = re.search(r"\((\d+(?:\s*,\s*\d+)*)\)", target)
        target_args = [int(x.strip()) for x in target_nums_match.group(1).split(',')] if target_nums_match else []
        if src_args and target_args:
            src_family = re.sub(r"\(.*$", "", row.source).upper()
            if src_family in ('VARCHAR', 'CHAR') and target_name in ('VARCHAR', 'CHAR') and target_args[0] < src_args[0]:
                raise ValueError(f"目标字段长度 {target_args[0]} 小于来源长度 {src_args[0]}")
            if src_family in ('DECIMAL', 'NUMERIC') and target_name in ('DECIMAL', 'NUMERIC') and (
                    len(target_args) >= 2 and len(src_args) >= 2 and
                    (target_args[0] < src_args[0] or target_args[1] < src_args[1])):
                raise ValueError('目标 NUMERIC/DECIMAL 精度或小数位缩小，可能造成数据损坏')
        return prefix + target + source[match.end():], row.source
    return definition, None
