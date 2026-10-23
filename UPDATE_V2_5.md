# SQLConverter v2.5 · PostgreSQL / OpenGauss 表级 UNIQUE 约束兼容性修复

## 问题与根因

在 `CREATE TABLE` 中，工具曾把 MySQL 联合唯一键的排序修饰符 `ASC` 原样复制到 PostgreSQL 风格的表级唯一约束：

```sql
CONSTRAINT "uk_sys_data_unit_tenant_code" UNIQUE ("tenant_id" ASC, "unit_code" ASC)
```

PostgreSQL 的 `UNIQUE (column_name, ...)` 约束不接受索引列级别的 `ASC/DESC`；某些非 B 兼容模式的 OpenGauss / GaussDB 也会以 `ASC/DESC is supported only in B-format database` 拒绝此约束。原始 `CREATE TABLE` 失败后，随后的大量 `INSERT` 因表不存在而连锁失败。

正确的表级约束输出：

```sql
CONSTRAINT "uk_sys_data_unit_tenant_code" UNIQUE ("tenant_id","unit_code")
```

**注意：** `CREATE INDEX ... (column DESC)` 属于独立索引语法，不能用全文件正则替换 `ASC/DESC`。

## 修改范围

- **`sql_converter/engine.py`**：校验唯一约束、主键的简单列清单；省略显式升序（默认就是 ASC）；普通索引保持 `ASC/DESC`。具名唯一索引中存在 `DESC` 时，转换为独立 `CREATE UNIQUE INDEX` 保留排序方向。
- 非法或不安全的形态（例如 `PRIMARY KEY (... DESC)`，或没有名称的 DESC 唯一约束）继续报 `BLOCKER`，不会直接静默改写。
- **`tests/test_postgres_constraints.py`**：新增 10 个回归测试，涵盖实际 `sys_data_unit` 例子、复合唯一约束、独立索引、主键、DESC、CHECK 和字面量。
- **`samples/postgres_constraint_smoke.sql`**：无需导入真实用户数据的快速建表兼容性测试。
- 保留 500 MiB 总文件限制、32 MiB 真正超长单条语句安全限制、流式扫描、取消机制及 v2.4 特殊 Navicat 字符串修复。

## 验证结果与边界

1. `python -m unittest discover -s tests -v`：113 项测试，其中 111 项通过，2 项 PySide6 GUI 测试因当前环境无 Qt 跳过。
2. 从真实 `iems_cloud.zip` (约 268.7 MiB) 流式扫描 672,390 条语句；64 个 `CREATE TABLE` 单独转换：0 BLOCKER；统计到 39 行表级唯一约束，无 `UNIQUE (... ASC/DESC ...)` 或 `PRIMARY KEY (... ASC/DESC ...)`。
3. 完整的 268.7 MiB 端到端转换在当前执行时限内**尚未完成**，因此不能称作本轮大文件全流程通过；较小的 `iems_cloud(1).sql` 回归转换 167 条 SQL，0 BLOCKER。
4. 没有数据库连接，**未在 PostgreSQL / OpenGauss / GaussDB 实际执行生成脚本**。运行结果、字段默认值、排序规则、唯一性逻辑仍需目标库验证。
5. 支持的转换方向依旧是 MySQL ↔ OpenGauss。输出 DDL 以通用 PostgreSQL 语法为优先，但它不是“PostgreSQL 全方言已完整支持”的承诺。

## 更新方式

把补丁 zip 中的文件上传到你的 GitHub `lhuat/sql-converter` 仓库对应路径，保留目录结构；原有 `.github/workflows/build-windows.yml` 无需修改。提交后 GitHub Actions 将重新执行测试并自动构建 Windows EXE。

建议 commit：`fix: make UNIQUE constraints portable across PostgreSQL and openGauss`

**请在全新空测试库中先运行 samples/postgres_constraint_smoke.sql；成功后，再测试真实建表 SQL，最后才执行数据导入。** 不要直接重放会先 DROP TABLE 的全库脚本到有真实数据的数据库。
