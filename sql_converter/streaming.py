"""Bounded-memory input scanner for large local SQL dumps.

Statements are streamed from disk and only one bounded SQL statement is held
in memory at a time. A statement exceeding the safety threshold is reported
as a blocker instead of silently truncated or output as executable SQL.
"""
from __future__ import annotations

import codecs
import re
from pathlib import Path
from typing import Callable, Iterator, TextIO

from .scanner import Statement

# App file-size policy: 500 MiB (524,288,000 bytes), inclusive.
MAX_SQL_FILE_BYTES = 500 * 1024 * 1024
# A 500 MiB dump is supported without attempting to parse a 500 MiB single
# INSERT as one Python string. Extended INSERTs above this size need splitting.
MAX_STATEMENT_BYTES = 32 * 1024 * 1024
CHUNK_CHARS = 1024 * 1024
LOOKAHEAD_CHARS = 512


def validate_file_size(path: Path, max_bytes: int = MAX_SQL_FILE_BYTES) -> int:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f'SQL 文件大小为 {size / 1024**2:.2f} MiB，超过 500 MiB 上限（524,288,000 字节）')
    if size == 0:
        raise ValueError('SQL 文件为空')
    return size


def detect_encoding(path: Path, requested: str = 'auto') -> str:
    """Validate encoding incrementally rather than loading the whole dump."""
    if requested != 'auto':
        codecs.lookup(requested)  # fail fast on unsupported encodings
        return requested
    for candidate in ('utf-8-sig', 'gb18030'):
        decoder = codecs.getincrementaldecoder(candidate)(errors='strict')
        try:
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    decoder.decode(chunk, final=False)
                decoder.decode(b'', final=True)
            return candidate
        except UnicodeDecodeError:
            continue
    raise ValueError('无法识别 SQL 文件编码，请手动选择正确编码')


def find_unsafe_dump_directive(path: Path, encoding: str) -> tuple[str, str] | None:
    """Fail closed for client-only dump modes before emitting partial SQL.

    Mirrors convert_text's intentionally conservative line-anchored checks.
    A rolling buffer keeps memory bounded even for files with few newlines.
    """
    patterns = (
        ('DELIMITER_MODE', re.compile(r'(?mi)^\s*DELIMITER\s+\S+')),
        ('COPY_STDIN', re.compile(r'(?mi)^[ \t]*COPY\b')),
        ('MYSQL_SQL_MODE', re.compile(r'(?mi)^\s*SET\s+[^;\n]*SQL_MODE[^;\n]*NO_BACKSLASH_ESCAPES')),
        ('PG_STRING_MODE', re.compile(r'(?mi)^\s*SET\s+standard_conforming_strings\s*=\s*(?:off|false|0)\s*;')), 
    )
    carry = ''
    with path.open('r', encoding=encoding, errors='strict', newline='') as handle:
        while chunk := handle.read(CHUNK_CHARS):
            text = carry + chunk
            for code, pattern in patterns:
                if pattern.search(text):
                    return code, ('包含 DELIMITER 客户端指令；为防止拆坏过程体，已终止整个文件转换'
                                  if code == 'DELIMITER_MODE' else
                                  '发现 NO_BACKSLASH_ESCAPES 模式，字符串需要另一套词法解析' if code == 'MYSQL_SQL_MODE' else
                                  'standard_conforming_strings=off 会改变字符串语义，已拒绝转换' if code == 'PG_STRING_MODE' else
                                  '包含 COPY 客户端导入命令；请重新导出为 INSERT 格式')
            # preserve enough chars to recognize a match crossing a chunk edge
            carry = text[-2048:]
    return None


# One streaming preflight pass validates the selected encoding and checks
# dump directives. Previous versions decoded the complete file once and then
# reopened it for directive detection, doubling preflight disk I/O.
_UNSAFE_PATTERNS = (
    ('DELIMITER_MODE', re.compile(r'(?mi)^\s*DELIMITER\s+\S+')),
    ('COPY_STDIN', re.compile(r'(?mi)^[ \t]*COPY\b')),
    ('MYSQL_SQL_MODE', re.compile(r'(?mi)^\s*SET\s+[^;\n]*SQL_MODE[^;\n]*NO_BACKSLASH_ESCAPES')),
        ('PG_STRING_MODE', re.compile(r'(?mi)^\s*SET\s+standard_conforming_strings\s*=\s*(?:off|false|0)\s*;')),
)


def preflight_sql(path: Path, requested: str = 'auto',
                  on_chunk: Callable[[], None] | None = None) -> tuple[str, tuple[str, str] | None]:
    """Return verified encoding and an optional blocking dump directive.

    Performs incremental Unicode decoding and recognizes known client-side
    directives in one bounded-memory read. Retry with GB18030 only when the
    complete UTF-8 stream fails strict decoding, not based on a small sample.
    """
    candidates = ('utf-8-sig', 'gb18030') if requested == 'auto' else (requested,)
    for encoding in candidates:
        decoder = codecs.getincrementaldecoder(encoding)(errors='strict')
        carry = ''
        directive = None
        try:
            with path.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    if on_chunk:
                        on_chunk()
                    decoded = decoder.decode(chunk, final=False)
                    if directive is None:
                        text = carry + decoded
                        for code, pattern in _UNSAFE_PATTERNS:
                            if pattern.search(text):
                                message = ('包含 DELIMITER 客户端指令；为防止拆坏过程体，已终止整个文件转换'
                                           if code == 'DELIMITER_MODE' else
                                           '发现 NO_BACKSLASH_ESCAPES 模式，字符串需要另一套词法解析'
                                           if code == 'MYSQL_SQL_MODE' else
                                           'standard_conforming_strings=off 会改变字符串语义，已拒绝转换'
                                           if code == 'PG_STRING_MODE' else
                                           '包含 COPY 客户端导入命令；请重新导出为 INSERT 格式')
                                directive = (code, message)
                                break
                        carry = text[-2048:]
                final = decoder.decode(b'', final=True)
                if directive is None and final:
                    text = carry + final
                    for code, pattern in _UNSAFE_PATTERNS:
                        if pattern.search(text):
                            message = ('包含 DELIMITER 客户端指令；为防止拆坏过程体，已终止整个文件转换'
                                       if code == 'DELIMITER_MODE' else
                                       '发现 NO_BACKSLASH_ESCAPES 模式，字符串需要另一套词法解析'
                                       if code == 'MYSQL_SQL_MODE' else
                                       'standard_conforming_strings=off 会改变字符串语义，已拒绝转换'
                                       if code == 'PG_STRING_MODE' else
                                       '包含 COPY 客户端导入命令；请重新导出为 INSERT 格式')
                            directive = (code, message)
                            break
            return encoding, directive
        except UnicodeDecodeError:
            if requested != 'auto':
                raise
            continue
    raise ValueError('无法识别 SQL 文件编码，请手动选择正确编码')


# Match SQL delimiter characters using the C regex engine. On ordinary SQL text
# (especially multi-row INSERT payloads) this skips long stretches in one go.
_NORMAL_EVENTS = re.compile(r"[\n'\"`;/#$-]")
_QUOTED_EVENTS = {
    'single': re.compile(r"[\\\n']"),
    'double': re.compile(r'[\\\n"]'),
    'backtick': re.compile(r'[\n`]'),
}
_DOLLAR_OPEN = re.compile(r'\$[a-zA-Z_0-9]*\$')


def iter_sql_stream(handle: TextIO, *, chunk_chars: int = CHUNK_CHARS,
                    postgres_mode: bool = False,
                    max_statement_bytes: int = MAX_STATEMENT_BYTES,
                    on_chars: Callable[[int], None] | None = None) -> Iterator[Statement]:
    """Yield bounded SQL statements with event-based scanning.

    The lexer jumps directly to quote/comment/newline/delimiter events instead
    of evaluating every ordinary character in Python. It keeps state across
    read boundaries, including MySQL backslash escapes and dollar quotes.
    """
    if chunk_chars <= LOOKAHEAD_CHARS:
        raise ValueError('chunk_chars must exceed parser lookahead')
    buf = handle.read(chunk_chars)
    eof = not buf
    pos = begin = 0
    consumed = 0
    line = start_line = 1
    state = 'normal'
    dollar_tag = ''
    escaped_string = True
    parts: list[str] = []
    length = 0
    oversized = False

    def append(value: str) -> None:
        nonlocal oversized, length
        if not value or oversized:
            return
        part_bytes = len(value.encode('utf-8'))
        if length + part_bytes > max_statement_bytes:
            oversized = True
            parts.clear()
            length = 0
        else:
            parts.append(value)
            length += part_bytes

    while True:
        limit = len(buf) if eof else max(0, len(buf) - LOOKAHEAD_CHARS)
        if pos >= limit and not eof:
            append(buf[begin:pos])
            consumed += pos
            if on_chars:
                on_chars(consumed)
            new_text = handle.read(chunk_chars)
            buf = buf[pos:] + new_text
            pos = begin = 0
            eof = not new_text
            continue
        if pos >= len(buf):
            break
        if state == 'normal':
            match = _NORMAL_EVENTS.search(buf, pos, limit)
            if not match:
                pos = limit
                continue
            pos = match.start()
            c = buf[pos]
            nxt = buf[pos + 1] if pos + 1 < len(buf) else ''
            if c == '\n':
                line += 1
            elif c == "'":
                state = 'single'
                escaped_string = not postgres_mode or (pos > 0 and buf[pos-1] in 'Ee' and (pos == 1 or not (buf[pos-2].isalnum() or buf[pos-2] in '_$')))
            elif c == '"':
                state = 'double'
            elif c == '`':
                state = 'backtick'
            elif c == '-' and nxt == '-' and (pos + 2 == len(buf) or buf[pos + 2].isspace()):
                state = 'line_comment'
                pos += 1
            elif c == '#':
                state = 'line_comment'
            elif c == '/' and nxt == '*':
                state = 'block_comment'
                pos += 1
            elif c == '$':
                tag = _DOLLAR_OPEN.match(buf, pos)
                if tag:
                    dollar_tag = tag.group(0)
                    state = 'dollar'
                    pos += len(dollar_tag) - 1
            elif c == ';':
                append(buf[begin:pos + 1])
                if oversized:
                    yield Statement('', start_line, too_large=True)
                elif parts:
                    # Avoid repeated joins for statements that fit one chunk.
                    statement = ''.join(parts)
                    if statement.strip():
                        yield Statement(statement, start_line)
                parts = []
                length = 0
                oversized = False
                begin = pos + 1
                start_line = line
            pos += 1
        elif state in ('single', 'double', 'backtick'):
            match = _QUOTED_EVENTS[state].search(buf, pos, limit)
            if not match:
                pos = limit
                continue
            pos = match.start()
            c = buf[pos]
            quote = {'single': "'", 'double': '"', 'backtick': '`'}[state]
            nxt = buf[pos + 1] if pos + 1 < len(buf) else ''
            if c == '\n':
                line += 1
            elif c == '\\' and (state == 'single' and escaped_string or state == 'double' and not postgres_mode) and nxt:
                if nxt == '\n':
                    line += 1
                pos += 1
            elif c == quote:
                if nxt == quote:
                    pos += 1
                else:
                    state = 'normal'
            pos += 1
        elif state == 'line_comment':
            end = buf.find('\n', pos, limit)
            if end == -1:
                pos = limit
            else:
                pos = end + 1
                line += 1
                state = 'normal'
        elif state in ('block_comment', 'dollar'):
            terminator = '*/' if state == 'block_comment' else dollar_tag
            end = buf.find(terminator, pos, limit)
            if end == -1:
                line += buf.count('\n', pos, limit)
                pos = limit
            else:
                line += buf.count('\n', pos, end + len(terminator))
                pos = end + len(terminator)
                state = 'normal'
    append(buf[begin:])
    consumed += len(buf)
    if on_chars:
        on_chars(consumed)
    if oversized:
        yield Statement('', start_line, too_large=True)
    elif parts:
        tail = ''.join(parts)
        if tail.strip():
            yield Statement(tail, start_line, unterminated=state not in ('normal', 'line_comment'))
