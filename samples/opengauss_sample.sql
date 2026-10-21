-- 演示用 SQL（请使用 gs_dump --inserts 导出）
DROP TABLE IF EXISTS "device";
CREATE TABLE "device" (
  "id" BIGSERIAL PRIMARY KEY,
  "name" VARCHAR(128) NOT NULL,
  "capacity" NUMERIC(14,4),
  "created_at" TIMESTAMP(6)
);
INSERT INTO "device" ("id","name","capacity") VALUES (1,'demo',18.2500);
