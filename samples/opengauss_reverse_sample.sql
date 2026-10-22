-- 独立构造的反向转换演示数据，不包含真实业务信息。
CREATE TABLE "example" (
    "id" BIGSERIAL PRIMARY KEY,
    "name" VARCHAR(80) NOT NULL,
    "payload" BYTEA,
    "content" JSONB,
    "created_at" TIMESTAMP(6) WITHOUT TIME ZONE
);
COMMENT ON TABLE "example" IS '示例表';
COMMENT ON COLUMN "example"."name" IS '示例名称';
CREATE UNIQUE INDEX "uq_example_active" ON "example" ("name", ((CASE WHEN "id" > 0 THEN "id" ELSE NULL END)));
INSERT INTO "example" ("id", "name", "payload", "content", "created_at") VALUES
(1, E'hello\\nworld', '\xDEADBEEF', '{"ok": true}', '2026-10-22 11:00:00');
