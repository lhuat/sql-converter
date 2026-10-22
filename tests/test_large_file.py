"""Bounded-memory SQL import and 500 MiB cap regression tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from sql_converter.engine import convert_file, convert_text
from sql_converter.scanner import split_sql
from sql_converter.streaming import (
    MAX_SQL_FILE_BYTES, MAX_STATEMENT_BYTES, detect_encoding, iter_sql_stream, validate_file_size,
)


class LargeSqlTests(unittest.TestCase):
    def test_500_mib_limit_inclusive_and_above_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            p = d / 'virtual.sql'
            with p.open('wb') as handle:
                handle.truncate(MAX_SQL_FILE_BYTES)
            self.assertEqual(validate_file_size(p), MAX_SQL_FILE_BYTES)
            with p.open('r+b') as handle:
                handle.truncate(MAX_SQL_FILE_BYTES + 1)
            with self.assertRaisesRegex(ValueError, '超过 500 MiB'):
                validate_file_size(p)
            with self.assertRaisesRegex(ValueError, '500 MiB'):
                convert_file(p, d, 'mysql_to_opengauss')

    def test_streaming_matches_regular_scanner_with_small_boundaries(self):
        source = ("-- 备注; 不拆分\nCREATE TABLE `demo` (`id` int, `note` text);\n"
                  "INSERT INTO `demo` VALUES (1, 'a;b'), (2, '中文;测试');\n"
                  "/* xx; */ INSERT INTO `demo` VALUES (3, 'has ''q''');\n"
                  "-- trailing comment\n")
        actual = list(iter_sql_stream(StringIO(source), chunk_chars=540))
        expected = split_sql(source)
        self.assertEqual([(s.sql, s.line) for s in actual], [(s.sql, s.line) for s in expected])

    def test_streaming_lexical_boundaries(self):
        # position SQL punctuation intentionally at each chunk boundary
        for n in (500, 509, 511, 513, 1000):
            with self.subTest(n=n):
                sql = "/*" + "x" * n + "; not sql */\n" + "INSERT INTO `t` VALUES('a;''b');\n"
                self.assertEqual([s.sql for s in iter_sql_stream(StringIO(sql), chunk_chars=545)],
                                 [s.sql for s in split_sql(sql)])

    def test_dollar_quotes_maintained(self):
        sql = "CREATE FUNCTION x() RETURNS void AS $$ BEGIN RAISE NOTICE 'a;b'; END $$ LANGUAGE plpgsql;"
        actual = list(iter_sql_stream(StringIO(sql), chunk_chars=600))
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0].sql, sql)

    def test_streaming_handles_gb18030_and_utf8(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            for enc in ('utf-8', 'gb18030'):
                with self.subTest(enc=enc):
                    p = d / (enc + '.sql')
                    p.write_bytes("CREATE TABLE `t` (`id` INT COMMENT '测试');".encode(enc))
                    self.assertEqual(detect_encoding(p), 'utf-8-sig' if enc == 'utf-8' else 'gb18030')
                    output, report_path, report = convert_file(p, d, 'mysql_to_opengauss')
                    self.assertTrue(report.ready)
                    self.assertIn('测试', output.read_text(encoding='utf-8-sig'))

    def test_many_statements_report_and_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            p = d / 'many.sql'
            with p.open('w', encoding='utf-8') as f:
                f.write('CREATE TABLE `t` (`id` INT);\n')
                for i in range(10000):
                    f.write(f'INSERT INTO `t` VALUES ({i});\n')
            update = []
            out, report_path, report = convert_file(p, d, 'mysql_to_opengauss', progress=lambda a,b: update.append((a,b)))
            self.assertEqual(report.converted, 10001)
            self.assertTrue(report.ready)
            self.assertIn('VALUES (9999)', out.read_text(encoding='utf-8-sig'))
            self.assertEqual(update[-1], (100,100))
            self.assertEqual(json.loads(report_path.read_text(encoding='utf-8'))['converted'], 10001)

    def test_oversized_single_statement_is_blocked_not_truncated(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            p = d / 'huge.sql'
            p.write_text("INSERT INTO `t` VALUES('" + ('x' * 1500) + "');", encoding='utf-8')
            # Test the stream scanner directly with a low threshold instead of
            # allocating a real 32 MiB test statement.
            with p.open('r', encoding='utf-8') as handle:
                stmts = list(iter_sql_stream(handle, chunk_chars=600, max_statement_bytes=1024))
            self.assertEqual(len(stmts), 1)
            self.assertTrue(stmts[0].too_large)
            self.assertEqual(stmts[0].sql, '')

    def test_unsafe_dump_modes_abort_without_earlier_executable_sql(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            for name, input_text in (
                ('delimiter.sql', 'CREATE TABLE `t` (`id` INT);\nDELIMITER $$\nCREATE PROCEDURE x() SELECT 1;'),
                ('copy.sql', 'CREATE TABLE t (id INT);\nCOPY t FROM STDIN;\n1\n\\.\n'),
            ):
                with self.subTest(name=name):
                    source = d / name
                    source.write_text(input_text, encoding='utf-8')
                    output, report_file, report = convert_file(source, d, 'mysql_to_opengauss')
                    self.assertFalse(report.ready)
                    self.assertNotIn('CREATE TABLE', output.read_text(encoding='utf-8-sig'))
                    self.assertEqual(report.total, 1)
                    self.assertEqual(output.read_bytes().count(b'\xef\xbb\xbf'), 1)

    def test_real_over_limit_statement_blocks_and_keeps_memory_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            src = d / 'oversized.sql'
            with src.open('w', encoding='ascii') as f:
                f.write("INSERT INTO `t` VALUES('")
                # A single syntactically valid INSERT of 33 MiB is too large
                # to process safely using the current per-statement parser.
                for _ in range(33):
                    f.write('x' * (1024 * 1024))
                f.write("');\n")
            out, rep, report = convert_file(src, d, 'mysql_to_opengauss')
            self.assertEqual(report.total, 1)
            self.assertEqual(report.blocked, 1)
            self.assertTrue(any(f.code == 'STATEMENT_TOO_LARGE' for f in report.findings))
            self.assertNotIn('INSERT INTO', out.read_text(encoding='utf-8-sig'))
            self.assertEqual(out.read_bytes().count(b'\xef\xbb\xbf'), 1)

    def test_failed_decode_does_not_leave_temporary_sql(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            src = d / 'invalid.sql'
            src.write_bytes(b'INSERT INTO `t` VALUES (1);\n' + bytes([0xff]))
            with self.assertRaises(UnicodeDecodeError):
                convert_file(src, d, 'mysql_to_opengauss', encoding='utf-8')
            self.assertEqual(sorted(p.name for p in d.iterdir()), ['invalid.sql'])

    def test_report_is_bounded_for_many_unsupported_statements(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            src = d / 'bad.sql'
            src.write_text('CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO SELECT 1;\n' * 5100,
                           encoding='utf-8')
            output, report_path, report = convert_file(src, d, 'mysql_to_opengauss')
            self.assertEqual(report.blocked, 5100)
            self.assertEqual(len(report.findings), 5000)
            self.assertEqual(report.omitted_findings, 100)
            self.assertEqual(json.loads(report_path.read_text(encoding='utf-8'))['omitted_findings'], 100)

    def test_multibyte_and_line_numbers(self):
        source = "-- 你好\nINSERT INTO `t` VALUES('测试');\nINSERT INTO `t` VALUES('b');\n"
        stream = list(iter_sql_stream(StringIO(source), chunk_chars=550))
        self.assertEqual([(s.line, s.too_large) for s in stream], [(1, False), (2, False)])
        out, report = convert_text(source, 'mysql_to_opengauss')
        self.assertTrue(report.ready)


if __name__ == '__main__':
    unittest.main()
