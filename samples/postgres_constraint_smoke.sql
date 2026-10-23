-- Compatible CREATE TABLE / CREATE INDEX smoke test for PostgreSQL and openGauss.
-- Run only in a TEST database. The temporary table is removed on COMMIT/ROLLBACK.
BEGIN;
CREATE TEMP TABLE "sqlconverter_constraint_smoke" (
    "id" BIGINT NOT NULL,
    "tenant_id" BIGINT NOT NULL,
    "unit_code" VARCHAR(64) NOT NULL,
    PRIMARY KEY ("id"),
    CONSTRAINT "uk_sqlconv_smoke_tenant_code" UNIQUE ("tenant_id", "unit_code")
) ON COMMIT DROP;
CREATE INDEX "idx_sqlconv_smoke_unit_code" ON "sqlconverter_constraint_smoke" ("unit_code" ASC);
INSERT INTO "sqlconverter_constraint_smoke" ("id", "tenant_id", "unit_code") VALUES (1, 1, 'one');
SELECT COUNT(*) FROM "sqlconverter_constraint_smoke";
ROLLBACK;
