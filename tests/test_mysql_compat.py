import unittest
from sql_converter.mysql_compat import (_read_mysql_single, normalize_mysql_literals,
                                        validate_case_index_expression, LiteralConversionError)
from sql_converter.engine import convert_text

class MySqlCompatibilityTests(unittest.TestCase):
    def test_mysql_escape_exact_semantics(self):
        source = r"INSERT INTO `t` VALUES ('A\r\nB\t\\z\\\"Q\'');"
        expected, changed = normalize_mysql_literals(source)
        self.assertTrue(changed)
        self.assertIn("E'", expected)
        self.assertIn(r'\r\n', expected)
        self.assertIn(r'\t', expected)
        self.assertIn(r'\\z', expected)
        self.assertIn("Q''", expected)

    def test_mixed_json_mysql_escaping(self):
        source = r'''INSERT INTO `t` VALUES ('{"key":"x\\n","inner":"\\\""}');'''
        converted, report = convert_text(source, 'mysql_to_opengauss')
        self.assertTrue(report.ready, [(f.code,f.message) for f in report.findings])
        self.assertIn("E'", converted)
        self.assertIn('INSERT INTO "t"', converted)

    def test_utf8_introducer_in_check_and_case(self):
        src = """CREATE TABLE `t` (
         `id` bigint NOT NULL,
         `a` varchar(12) COLLATE utf8mb4_bin,
         `flag` smallint NOT NULL,
         PRIMARY KEY (`id`),
         UNIQUE KEY `uk` (`a`,((case when ((`flag` = 0) and (`a` <> _utf8mb4'')) then `a` else NULL end))),
         CONSTRAINT `chk` CHECK ((`a` <> _utf8mb4'bad'))
         ) ENGINE=InnoDB CHARSET=utf8mb4;"""
        sql, report = convert_text(src,'mysql_to_opengauss')
        self.assertTrue(report.ready, [(f.line,f.code,f.message) for f in report.findings])
        self.assertIn("<> ''",sql)
        self.assertNotIn('_utf8mb4',sql)
        self.assertIn('CREATE UNIQUE INDEX',sql)
        self.assertIn('CONSTRAINT "chk" CHECK',sql)

    def test_two_composite_logical_delete_index_forms(self):
        sql, report = convert_text('''CREATE TABLE `s` (
        `k` varchar(50), `flag` smallint,
        UNIQUE KEY `uk1` (`k`,((case when (`flag` = 0) then 1 else NULL end))),
        UNIQUE KEY `uk2` (((case when (`flag` = 0) then lower(trim(`k`)) else NULL end)))
        );''','mysql_to_opengauss')
        self.assertTrue(report.ready, [(f.line, f.code, f.message) for f in report.findings])
        self.assertEqual(sql.count('CREATE UNIQUE INDEX'),2)

    def test_unsafe_expression_not_relaxed(self):
        with self.assertRaises(LiteralConversionError):
            validate_case_index_expression('(CASE WHEN 1=1 THEN (SELECT secret) ELSE NULL END)')

    def test_binary_and_null_cannot_be_silently_coerced(self):
        for source in (r"INSERT INTO t VALUES ('a\0b');", r"INSERT INTO t VALUES ('a\Zb');"):
            with self.subTest(source=source):
                output, report = convert_text(source,'mysql_to_opengauss')
                self.assertEqual(report.blocked, 1)
                self.assertIn('MANUAL REVIEW REQUIRED',output)

    def test_mysql_unrecognized_backslash_escape(self):
        source = r"INSERT INTO t VALUES ('a\qb');"
        output, report = convert_text(source,'mysql_to_opengauss')
        self.assertTrue(report.ready)
        self.assertIn("'aqb'", output)

if __name__ == '__main__':unittest.main()

class MigrationGuardTests(unittest.TestCase):
    def test_no_backslash_escapes_mode_blocks(self):
        src = "SET SQL_MODE = 'NO_BACKSLASH_ESCAPES';\nINSERT INTO t VALUES ('abc');"
        _, result = convert_text(src, 'mysql_to_opengauss')
        self.assertEqual(result.blocked, 1)
        self.assertIn('MYSQL_SQL_MODE', [x.code for x in result.findings])

    def test_foreign_key_check_import_order_blocked(self):
        src = '''CREATE TABLE `child` (
          `id` BIGINT NOT NULL,
          `parent_id` BIGINT NOT NULL,
          CONSTRAINT `fk_p` FOREIGN KEY (`parent_id`) REFERENCES `parent` (`id`)
        ) ENGINE=InnoDB;'''
        _, result = convert_text(src, 'mysql_to_opengauss')
        self.assertEqual(result.blocked, 1)
        self.assertIn('FOREIGN_KEY_IMPORT_ORDER',[f.code for f in result.findings])

    def test_report_summary(self):
        _, result = convert_text('CREATE TABLE `t` (`a` VARCHAR(10) COLLATE utf8mb4_bin) ENGINE=InnoDB;', 'mysql_to_opengauss')
        self.assertTrue(result.ready)
        summary=result.to_dict()['finding_summary']
        self.assertTrue('WARNING:COLLATION_REVIEW' in summary)
        self.assertTrue('INFO:TABLE_OPTIONS' in summary)
