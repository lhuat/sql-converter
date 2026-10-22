"""Regression cases for fast scanning, unified preflight and safe cancel."""
from __future__ import annotations

import tempfile
import unittest
from io import StringIO
from pathlib import Path

from sql_converter.engine import ConversionCancelled, convert_file
from sql_converter.scanner import split_sql
from sql_converter.streaming import iter_sql_stream, preflight_sql


class PerformanceRegressionTests(unittest.TestCase):
    def test_lexical_events_across_small_chunks(self):
        # Covers SQL strings, escaped quotes, comments, dollar bodies and
        # newline tracking across arbitrary chunk boundaries.
        sql = ("-- 中文注释;\n"
               "CREATE TABLE `a``b` (\"x\" text);\n"
               "/* 多行;\n测试 */ INSERT INTO `a``b` VALUES ('xx;\\n', 'it''s', 'a\\'b');\n"
               "# comment;\n"
               "INSERT INTO `a``b` VALUES ('$tag$;','\\\\','😀');\n"
               "CREATE FUNCTION f() RETURNS text AS $tag$BEGIN\nRETURN ';';\nEND$tag$ LANGUAGE plpgsql;\n")
        expected = [(s.sql, s.line) for s in split_sql(sql)]
        for chunk in (513, 514, 515, 540, 600, 777, 1000, 4097):
            with self.subTest(chunk=chunk):
                actual = [(s.sql, s.line) for s in iter_sql_stream(StringIO(sql), chunk_chars=chunk)]
                self.assertEqual(actual, expected)

    def test_preflight_utf8_and_legacy_directives(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            examples = [
                ('ok', 'CREATE TABLE `t` (`value` text);\n', None),
                ('delimiter', 'CREATE TABLE `t` (id int);\nDELIMITER $$\n', 'DELIMITER_MODE'),
                ('copy', 'COPY t FROM STDIN;\n', 'COPY_STDIN'),
                ('mode', "SET sql_mode='NO_BACKSLASH_ESCAPES';\n", 'MYSQL_SQL_MODE'),
            ]
            for title, content, expected in examples:
                with self.subTest(title=title):
                    path = base / (title + '.sql')
                    path.write_text(content, encoding='utf-8')
                    encoding, directive = preflight_sql(path)
                    self.assertEqual(encoding, 'utf-8-sig')
                    self.assertEqual(directive[0] if directive else None, expected)
            gb = base / 'gb.sql'
            gb.write_bytes("INSERT INTO t VALUES ('中文');\n".encode('gb18030'))
            self.assertEqual(preflight_sql(gb)[0], 'gb18030')

    def test_cancel_while_checking_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'long.sql'
            with source.open('wb') as handle:
                handle.write(b'-- comments\n' * 110000)
            counts = [0]
            def is_cancelled():
                counts[0] += 1
                return counts[0] >= 2
            with self.assertRaises(ConversionCancelled):
                convert_file(source, base, 'mysql_to_opengauss', cancel_check=is_cancelled)
            self.assertEqual(sorted(p.name for p in base.iterdir()), ['long.sql'])

    def test_cancel_mid_stream_and_remove_temporary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'many.sql'
            with source.open('w', encoding='utf-8') as handle:
                handle.write('CREATE TABLE `t` (`id` INT);\n')
                for i in range(40000):
                    handle.write(f"INSERT INTO `t` VALUES ({i}, 'hello;world');\n")
            ticks = [0]
            def is_cancelled():
                ticks[0] += 1
                return ticks[0] > 10
            with self.assertRaises(ConversionCancelled):
                convert_file(source, base, 'mysql_to_opengauss', cancel_check=is_cancelled)
            self.assertEqual(sorted(p.name for p in base.iterdir()), ['many.sql'])

    def test_progress_updates_bounded_for_many_statements(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'many.sql'
            source.write_text('INSERT INTO `t` VALUES (1);\n' * 30000, encoding='utf-8')
            events = []
            output, report_path, report = convert_file(
                source, base, 'mysql_to_opengauss', progress=lambda done, total: events.append((done,total)))
            self.assertTrue(report.ready)
            self.assertEqual(report.converted, 30000)
            self.assertEqual(events[-1], (100, 100))
            self.assertLessEqual(len(events), 105)
            self.assertTrue(output.exists() and report_path.exists())
            import json
            saved = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual(saved['metrics']['input_bytes'], source.stat().st_size)
            self.assertGreater(saved['metrics']['average_mib_per_second'], 0)


if __name__ == '__main__':
    unittest.main()
