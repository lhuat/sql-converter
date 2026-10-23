"""Portable table-level key grammar for PostgreSQL and openGauss.

Regression: A mysql dump's UNIQUE KEY (`tenant_id` ASC, `code` ASC)
was rendered as UNIQUE ("tenant_id" ASC, "code" ASC), which is not
accepted by PostgreSQL CREATE TABLE and fails on non-B-format GaussDB.
"""
import re
import unittest
from sql_converter.engine import convert_text


class PortableConstraintTests(unittest.TestCase):
    def convert(self, sql):
        rendered, report = convert_text(sql, 'mysql_to_opengauss')
        self.assertEqual(report.blocked, 0, str([(f.code, f.message) for f in report.findings]))
        return rendered

    def test_real_first_error_sys_data_unit_unique_asc(self):
        source = '''CREATE TABLE `sys_data_unit` (
`id` bigint NOT NULL, `tenant_id` bigint NOT NULL,
`unit_code` varchar(64) NOT NULL,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_sys_data_unit_tenant_code` (`tenant_id` ASC,`unit_code` ASC) USING BTREE,
KEY `idx_tenant` (`tenant_id` ASC,`unit_code` DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;'''
        sql = self.convert(source)
        self.assertIn('CONSTRAINT "uk_sys_data_unit_tenant_code" UNIQUE ("tenant_id","unit_code")', sql)
        self.assertNotIn('UNIQUE ("tenant_id" ASC', sql)
        self.assertIn('CREATE INDEX "idx_tenant" ON "sys_data_unit" ("tenant_id" ASC,"unit_code" DESC)',sql)

    def test_primary_key_explicit_asc_is_omitted(self):
        sql = self.convert('CREATE TABLE `t` (`id` INT, `code` INT, PRIMARY KEY (`id` ASC,`code` ASC));')
        self.assertIn('PRIMARY KEY ("id","code")', sql)

    def test_named_unique_as_table_constraint_with_asc(self):
        sql = self.convert('CREATE TABLE `t` (`a` INT, `b` INT, CONSTRAINT `uk_pair` UNIQUE (`a` ASC,`b` ASC));')
        self.assertIn('CONSTRAINT "uk_pair" UNIQUE ("a","b")', sql)

    def test_unnamed_unique_asc(self):
        sql = self.convert('CREATE TABLE `t` (`a` INT, `b` INT, UNIQUE (`a` ASC,`b` ASC));')
        self.assertIn('UNIQUE ("a","b")', sql)

    def test_desc_unique_gets_independent_sorted_unique_index(self):
        sql = self.convert('CREATE TABLE `t` (`a` INT, `b` INT, UNIQUE KEY `uq` (`a` ASC,`b` DESC));')
        self.assertIn('CREATE UNIQUE INDEX "uq" ON "t" ("a" ASC,"b" DESC);', sql)
        self.assertNotIn('CONSTRAINT "uq" UNIQUE',sql)

    def test_named_unique_constraint_desc_goes_to_index(self):
        sql = self.convert('CREATE TABLE `t` (`a` INT, `b` INT, CONSTRAINT `uq` UNIQUE (`a` DESC,`b` ASC));')
        self.assertIn('CREATE UNIQUE INDEX "uq" ON "t" ("a" DESC,"b" ASC);',sql)

    def test_primary_desc_is_not_silently_removed(self):
        sql, report = convert_text('CREATE TABLE `t` (`a` INT, PRIMARY KEY (`a` DESC));','mysql_to_opengauss')
        self.assertFalse(report.ready)
        self.assertTrue(any(f.code == 'PRIMARY_KEY_DESC' for f in report.findings))

    def test_unnamed_unique_desc_is_not_silently_removed(self):
        sql, report = convert_text('CREATE TABLE `t` (`a` INT, UNIQUE (`a` DESC));','mysql_to_opengauss')
        self.assertFalse(report.ready)
        self.assertTrue(any(f.code == 'UNNAMED_UNIQUE_DESC' for f in report.findings))

    def test_normal_index_order_and_check_string_untouched(self):
        sql = self.convert('''CREATE TABLE `t` (`id` INT, `value` VARCHAR(20) DEFAULT 'ASC',
        CONSTRAINT `ck` CHECK (`value` <> 'DESC'),
        UNIQUE KEY `uq` (`id` ASC), KEY `ix` (`value` DESC));''')
        self.assertIn("DEFAULT 'ASC'",sql)
        self.assertIn("CHECK (\"value\" <> 'DESC')",sql)
        self.assertIn('CREATE INDEX "ix" ON "t" ("value" DESC)',sql)

    def test_no_sort_modifier_in_any_table_unique_constraint(self):
        cases = [
          'CREATE TABLE `t` (`a` INT, `b` INT, UNIQUE KEY `uq` (`a` ASC,`b` ASC));',
          'CREATE TABLE `t` (`a` INT, `b` INT, PRIMARY KEY (`a` ASC,`b` ASC));',
          'CREATE TABLE `t` (`a` INT, `b` INT, CONSTRAINT `uq` UNIQUE (`a` ASC,`b` ASC));',
        ]
        for case in cases:
            with self.subTest(case=case):
                sql = self.convert(case)
                self.assertIsNone(re.search(r'\b(?:UNIQUE|PRIMARY KEY)\s*\([^;]*?\b(?:ASC|DESC)\b', sql, re.I))


if __name__ == '__main__':
    unittest.main()
