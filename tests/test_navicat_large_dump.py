"""Regression for Navicat CASE-index escapes misclassified as a >32 MiB SQL."""
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from sql_converter.streaming import iter_sql_stream
from sql_converter.scanner import split_sql
from sql_converter.engine import convert_file, convert_text

DDL = (
    'DROP TABLE IF EXISTS `t`;\n'
    'CREATE TABLE `t` (\n'
    '  `id` bigint NOT NULL,\n'
    '  `v` varchar(30) NOT NULL,\n'
    '  `del_flag` int NOT NULL,\n'
    "  UNIQUE INDEX `uk`(`id`, ((case when ((`del_flag` = 0) and "
    "(`v` <> _utf8mb4\\'\\')) then `v` else NULL end)) ASC) USING BTREE\n"
    ') ENGINE = InnoDB CHARACTER SET = utf8mb4;\n'
)
VALUES = 'INSERT INTO `t` VALUES (1, \'abc\', 0);\n' * 500


class NavicatOversizeRegression(unittest.TestCase):
    def test_previous_unterminated_index_no_longer_swallow_sql(self):
        sql = DDL + VALUES
        expected_count = 502
        a = list(split_sql(sql))
        b = list(iter_sql_stream(StringIO(sql), chunk_chars=545,
                                 max_statement_bytes=2 * 1024))
        self.assertEqual(len(a), expected_count)
        self.assertEqual(len(b), expected_count)
        self.assertTrue(all(not s.too_large and not s.unterminated for s in b))
        self.assertEqual([s.sql for s in a], [s.sql for s in b])

    def test_safe_normalization_and_data_rows(self):
        output, report = convert_text(DDL + VALUES, 'mysql_to_opengauss')
        self.assertEqual(report.blocked, 0, [(f.code, f.message) for f in report.findings])
        self.assertEqual(report.converted, 502)
        self.assertEqual(output.count('INSERT INTO'), 500)
        self.assertIn("'')", output)
        self.assertTrue(any(f.code == 'NAVICAT_ESCAPED_EMPTY_LITERAL' for f in report.findings))

    def test_file_conversion_uses_same_rule(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'navicat.sql'
            src.write_text(DDL + VALUES, encoding='utf-8')
            sql, report_path, report = convert_file(src, Path(td), 'mysql_to_opengauss')
            self.assertEqual(report.blocked, 0)
            self.assertEqual(report.converted, 502)
            self.assertEqual(sql.read_text(encoding='utf-8-sig').count('INSERT INTO'), 500)

    def test_does_not_mutate_regular_data_or_open_gauss(self):
        from sql_converter.mysql_compat import normalize_navicat_index_quirk
        value = "INSERT INTO `t` VALUES ('hello _utf8mb4\\\\\\'\\\\\\' world');"
        self.assertEqual(normalize_navicat_index_quirk(value), (value, 0))
        plain = "CREATE TABLE `t` (`v` varchar(30) COMMENT '_utf8mb4\\\\\\'\\\\\\'');"
        self.assertEqual(normalize_navicat_index_quirk(plain), (plain, 0))

    def test_no_generic_escape_bypass(self):
        sql = "CREATE TABLE `t` (`id` int, `v` varchar(30) DEFAULT \\'foo\\'\\');"
        stream = list(iter_sql_stream(StringIO(sql), chunk_chars=545, max_statement_bytes=150))
        self.assertTrue(stream)
        # Unsupported escape forms must NOT be treated as successful equivalent DDL.
        _, report = convert_text(sql, 'mysql_to_opengauss')
        self.assertGreater(report.blocked, 0)


if __name__ == '__main__':
    unittest.main()

class PlainInsertFastPathTests(unittest.TestCase):
    def test_safe_rows_output_matches_standard_dml_rule(self):
        from sql_converter.engine import Context, Report, _fast_plain_insert, DmlRule
        from sql_converter.mysql_compat import normalize_mysql_literals
        from sql_converter.pg_compat import normalize_postgres_literals
        for direction, raw in (
            ('mysql_to_opengauss', "INSERT INTO `records` VALUES (1, 'a;b', NULL),(2, 'it''s', 2e3);"),
            ('opengauss_to_mysql', 'INSERT INTO "records" VALUES (1, \'a;b\', NULL),(2, \'it\'\'s\', 2e3);'),
        ):
            with self.subTest(direction=direction):
                ctx = Context(direction, {}, Report('mysql', 'opengauss'), 1)
                quick = _fast_plain_insert(raw, ctx)
                self.assertIsNotNone(quick)
                normalized, _ = (normalize_mysql_literals(raw) if direction == 'mysql_to_opengauss'
                                 else normalize_postgres_literals(raw))
                original = DmlRule().convert(normalized, ctx)
                self.assertEqual(quick, original)

    def test_fast_path_does_not_skip_complex_literal_validation(self):
        from sql_converter.engine import Context, Report, _fast_plain_insert
        c = Context('mysql_to_opengauss', {}, Report('mysql','opengauss'), 1)
        for raw in (
            r"INSERT INTO `t` VALUES ('a\\nb');",
            "INSERT INTO `t` VALUES (NOW());",
            "INSERT INTO `t` VALUES (X'4AFF');",
            "INSERT INTO `t` VALUES (_utf8mb4'a');",
            "INSERT INTO `t` VALUES (1) ON DUPLICATE KEY UPDATE id=1;",
        ):
            self.assertIsNone(_fast_plain_insert(raw,c), raw)
