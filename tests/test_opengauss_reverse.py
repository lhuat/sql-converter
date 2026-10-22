"""Regressions for openGauss/PG -> MySQL 8.0 offline dumps."""
from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from sql_converter.engine import convert_text, convert_file
from sql_converter.pg_compat import normalize_postgres_literals


class ReverseDialectTests(unittest.TestCase):
    def test_pg_standard_strings_escape_backslashes_for_mysql(self):
        sql = r'''INSERT INTO "t" VALUES ('C:\Users\bob\dir', 'can''t');'''
        out, report = convert_text(sql, 'opengauss_to_mysql')
        self.assertTrue(report.ready, report.findings)
        self.assertIn(r"'C:\\Users\\bob\\dir'", out)
        self.assertIn("'can''t'", out)
        self.assertIn('INSERT INTO `t`', out)

    def test_pg_e_strings_and_unicode(self):
        src = r'''INSERT INTO "t" VALUES (E'a\nb\t\\c', E'\u4E2D\u6587');'''
        out, report = convert_text(src, 'opengauss_to_mysql')
        self.assertTrue(report.ready, report.findings)
        self.assertIn(r"'a\nb\t\\c'", out)
        self.assertIn("'中文'", out)

    def test_pg_literal_parser_does_not_alter_comments_identifiers(self):
        src = "-- E'not SQL'\nINSERT INTO \"E'field'\" VALUES ('hello');"
        out, report = convert_text(src, 'opengauss_to_mysql')
        self.assertEqual(report.blocked, 0)
        self.assertIn('`E\'field\'`', out)

    def test_pg_invalid_escape_is_blocker(self):
        out, report = convert_text(r"INSERT INTO x VALUES (E'hello\q');", 'opengauss_to_mysql')
        self.assertFalse(report.ready)
        self.assertIn('STRING_LITERAL', [f.code for f in report.findings])

    def test_pg_null_escape(self):
        out, report = convert_text(r"INSERT INTO x VALUES (E'a\000b');", 'opengauss_to_mysql')
        self.assertTrue(report.ready)
        self.assertIn(r"'a\0b'", out)

    def test_pg_column_and_table_comments(self):
        src = '''CREATE TABLE "t" ("id" BIGSERIAL PRIMARY KEY, "name" VARCHAR(64) NOT NULL DEFAULT 'abc');
COMMENT ON TABLE "t" IS '数据表';
COMMENT ON COLUMN "t"."name" IS '含有备注的姓名';'''
        out, report = convert_text(src, 'opengauss_to_mysql')
        self.assertTrue(report.ready, report.findings)
        self.assertIn('ALTER TABLE `t` COMMENT = \'数据表\';', out)
        self.assertIn("MODIFY COLUMN `name` VARCHAR(64) NOT NULL DEFAULT 'abc' COMMENT '含有备注的姓名'", out)
        self.assertIn('BIGINT AUTO_INCREMENT PRIMARY KEY', out)

    def test_comment_column_without_table_schema_blocked(self):
        out, report = convert_text('COMMENT ON COLUMN "missing"."x" IS \'label\';','opengauss_to_mysql')
        self.assertFalse(report.ready)
        self.assertIn('COMMENT_COLUMN_SCHEMA', [f.code for f in report.findings])
        self.assertNotIn('MODIFY COLUMN', out)

    def test_quoted_identifier_and_comment_escaping(self):
        src='''CREATE TABLE "device" ("data" TEXT DEFAULT 'O''Reilly');
COMMENT ON COLUMN "device"."data" IS E'line1\\nline2';'''
        out, report = convert_text(src, 'opengauss_to_mysql')
        self.assertTrue(report.ready, report.findings)
        self.assertIn("COMMENT 'line1\\nline2'", out)

    def test_pg_regular_index_composite(self):
        src = 'CREATE UNIQUE INDEX "uk_pair" ON "device" ("tenant_id" ASC, "code" DESC);'
        out, report = convert_text(src,'opengauss_to_mysql')
        self.assertTrue(report.ready)
        self.assertIn('CREATE UNIQUE INDEX `uk_pair` ON `device` (`tenant_id` ASC, `code` DESC)',out)

    def test_pg_case_expression_index_for_mysql8(self):
        src='''CREATE UNIQUE INDEX "uk_active" ON "t" ("tenant_id", ((CASE WHEN "del_flag" = 0 THEN "code" ELSE NULL END)));'''
        out, report=convert_text(src,'opengauss_to_mysql')
        self.assertTrue(report.ready, report.findings)
        self.assertIn('`tenant_id`, (CASE WHEN `del_flag` = 0 THEN `code` ELSE NULL END)',out)
        self.assertIn('MYSQL_FUNCTION_INDEX', [f.code for f in report.findings])

    def test_partial_index_blocked_not_silently_equated(self):
        src='CREATE UNIQUE INDEX "uk_active" ON "t" ("code") WHERE "del_flag" = 0;'
        out,report=convert_text(src,'opengauss_to_mysql')
        self.assertFalse(report.ready)
        self.assertIn('PARTIAL_INDEX', [f.code for f in report.findings])
        self.assertNotIn('CREATE UNIQUE INDEX',out)

    def test_pg_cast_int(self):
        out,report=convert_text('INSERT INTO "t" ("id") VALUES (1::integer);','opengauss_to_mysql')
        self.assertTrue(report.ready, report.findings)
        self.assertIn('CAST(1 AS SIGNED)',out)

    def test_pg_timestamp_without_zone(self):
        src='CREATE TABLE "t" ("created" TIMESTAMP(6) WITHOUT TIME ZONE);'
        out,report=convert_text(src,'opengauss_to_mysql')
        self.assertTrue(report.ready, report.findings)
        self.assertIn('DATETIME(6)',out)
        self.assertNotIn('WITHOUT TIME ZONE',out)

    def test_timestamptz_must_block(self):
        src='CREATE TABLE "t" ("created" TIMESTAMP WITH TIME ZONE);'
        _,report=convert_text(src,'opengauss_to_mysql')
        self.assertFalse(report.ready)
        self.assertIn('TIMEZONE_TYPE',[f.code for f in report.findings])

    def test_unbounded_numeric_blocked(self):
        _,r=convert_text('CREATE TABLE "t" ("n" NUMERIC);','opengauss_to_mysql')
        self.assertIn('UNBOUNDED_NUMERIC',[f.code for f in r.findings])

    def test_numeric_outside_mysql_precision_blocked(self):
        _,r=convert_text('CREATE TABLE "t" ("n" NUMERIC(70,40));','opengauss_to_mysql')
        self.assertIn('MYSQL_DECIMAL_RANGE',[f.code for f in r.findings])

    def test_pg_uuid_mapping(self):
        out,r=convert_text('CREATE TABLE "t" ("id" UUID);','opengauss_to_mysql')
        self.assertTrue(r.ready,r.findings)
        self.assertIn('CHAR(36)',out)

    def test_pg_jsonb_semantics_warned(self):
        out,r=convert_text('CREATE TABLE "t" ("details" JSONB);','opengauss_to_mysql')
        self.assertTrue(r.ready,r.findings)
        self.assertIn('JSONB_SEMANTICS',[f.code for f in r.findings])

    def test_identity_without_key_blocked(self):
        _,r=convert_text('CREATE TABLE "t" ("id" SERIAL, "name" TEXT);','opengauss_to_mysql')
        self.assertIn('MYSQL_IDENTITY_KEY',[f.code for f in r.findings])

    def test_pg_specific_dml_operators_are_blocked(self):
        for statement in (
            "UPDATE \"t\" SET \"name\" = \"name\" || 'suffix';",
            "DELETE FROM \"t\" WHERE \"name\" ILIKE 'abc%';",
            "UPDATE \"t\" SET \"json\" = \"json\" #>> '{a}';",
            'DELETE FROM "t" USING "other" WHERE "t"."id" = "other"."id";',
        ):
            with self.subTest(statement=statement):
                _, report = convert_text(statement, 'opengauss_to_mysql')
                self.assertFalse(report.ready, report.findings)

    def test_pg_bytea_hex_values_to_mysql_blob(self):
        source = r'''CREATE TABLE "bin" ("id" INTEGER, "payload" BYTEA);
INSERT INTO "bin" ("id","payload") VALUES (1,'\xDEADBEEF'),(2,NULL);'''
        out, r = convert_text(source, 'opengauss_to_mysql')
        self.assertTrue(r.ready, r.findings)
        self.assertIn('`payload` LONGBLOB', out)
        self.assertIn("X'DEADBEEF'", out)
        self.assertIn('(2, NULL)', out)

    def test_pg_bytea_non_hex_format_blocked(self):
        source = "CREATE TABLE \"bin\" (\"payload\" BYTEA); INSERT INTO \"bin\" VALUES ('hello');"
        out, r = convert_text(source, 'opengauss_to_mysql')
        self.assertFalse(r.ready)
        self.assertIn('BYTEA_INSERT_FORMAT', [f.code for f in r.findings])
        self.assertNotIn('INSERT INTO `bin`', out)

    def test_pg_backslash_before_terminal_quote(self):
        source = "INSERT INTO \"x\" VALUES ('ends\\');"
        out, r = convert_text(source, 'opengauss_to_mysql')
        self.assertTrue(r.ready, r.findings)
        self.assertIn(r"'ends\\'", out)

    def test_pg_bytea_with_position_columns(self):
        source = r'''CREATE TABLE "bin" ("id" INTEGER, "payload" BYTEA);
INSERT INTO "bin" VALUES (1,'\xAABB');'''
        out, r = convert_text(source,'opengauss_to_mysql')
        self.assertTrue(r.ready,r.findings)
        self.assertIn("X'AABB'",out)

    def test_pg_string_mode_off_blocks_text_and_file(self):
        content="SET standard_conforming_strings = off;\nINSERT INTO \"t\" VALUES ('a\\n');"
        _,r=convert_text(content,'opengauss_to_mysql')
        self.assertIn('PG_STRING_MODE',[f.code for f in r.findings])
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'source.sql';p.write_text(content,encoding='utf-8')
            _,_,rf=convert_file(p,Path(tmp),'opengauss_to_mysql')
            self.assertIn('PG_STRING_MODE',[f.code for f in rf.findings])

    def test_file_reverse_no_loss_of_comment_statements(self):
        src='''CREATE TABLE "t" ("id" BIGSERIAL PRIMARY KEY, "a" TEXT);
COMMENT ON COLUMN "t"."a" IS '测试';
INSERT INTO "t" VALUES (1,'汉字');'''
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp); inp=d/'demo.sql';inp.write_text(src,encoding='utf-8')
            target,_,r=convert_file(inp,d,'opengauss_to_mysql')
            self.assertTrue(r.ready,r.findings)
            result=target.read_text(encoding='utf-8-sig')
            self.assertIn('MODIFY COLUMN `a` LONGTEXT COMMENT',result)
            self.assertIn("VALUES (1,'汉字')",result)


if __name__ == '__main__':
    unittest.main()
