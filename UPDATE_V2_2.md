# SQLConverter v2.2 · 界面优化与 OpenGauss → MySQL 转换增强

## 一、界面调整（按已确认的 UI 效果）

- 产品展示文字统一使用 **OpenGauss**（内部规则标识、输出文件名仍保持小写 `opengauss`）。
- 文件、输出路径、文件名输入框使用统一的左侧标签列及右侧操作按钮列。
- 来源类型列减少多余留白，`UNSIGNED` 在本列靠右对齐；不会把无符号标记误当成新类型。
- 数据类型映射采用白色 / 浅灰交替行；可编辑下拉框与其所在行使用相同背景色。
- 标题、表格上下保留间距；进度条与“转换结果与语法报告”之间增加间距。
- “耗时 / 平均处理速度”独立展示在日志文本框上方，与日志左侧内容靠齐，不重复挤在日志文本中。
- 文件最多 **500 MiB**，单条 SQL 仍为 **32 MiB** 限制；保留异步转换和取消功能。

## 二、反向转换增加的处理规则

| OpenGauss 来源 | MySQL 目标 | 策略 |
| --- | --- | --- |
| `SMALLSERIAL`、`SERIAL`、`BIGSERIAL` | 对应整数类型 + `AUTO_INCREMENT` | 必须具备索引，否则阻断 |
| `GENERATED ... AS IDENTITY` | `AUTO_INCREMENT` | 记录风险，须检查序列起点和主键条件 |
| `TIMESTAMP WITHOUT TIME ZONE` | `DATETIME`（保留小数秒精度） | 支持 |
| `TIMESTAMP WITH TIME ZONE` | — | 阻断，不静默丢失时区 |
| `NUMERIC(p,s)` | `DECIMAL(p,s)` | 保留精度，拒绝超出 MySQL DECIMAL 范围 |
| 无界 `NUMERIC` | — | 阻断，目标有界精度无法保证数据范围 |
| `UUID` | `CHAR(36)` | 转换文本表示，仍需确认 UUID 格式和索引语义 |
| `JSONB` | `JSON` | 转换并提示 JSONB 规范化语义差异 |
| `BYTEA` | `LONGBLOB` | 兼容 `pg_dump` 十六进制值；其他编码阻断 |
| `COMMENT ON TABLE` | `ALTER TABLE ... COMMENT = ...` | 支持 |
| `COMMENT ON COLUMN` | `ALTER TABLE ... MODIFY COLUMN ... COMMENT ...` | 使用此前记录的完整列定义；没有定义则阻断 |
| `CREATE [UNIQUE] INDEX` | MySQL 索引语法 | 保留名称、字段顺序、ASC/DESC |
| 特定 `CASE WHEN ... ELSE NULL` 函数索引 | MySQL 8.0.13+ 函数索引 | 支持安全子集并发出版本兼容警告 |
| `WHERE` 部分索引 | — | 仍然阻断，避免误改变唯一性 |
| 常规单引号、`E'...'` | MySQL 单引号 | 转换反斜杠、单引号、换行、Unicode 转义 |
| 受限简单 `::integer` 等显式转换 | `CAST(expr AS type)` | 仅简单数值/标识符形式，其他保守阻断 |
| `ILIKE`、`||`、专有 JSON 运算符 | — | 阻断，不能直接原样输出 |
| `UPDATE ... FROM`、`DELETE ... USING` | — | 阻断，需要 MySQL 多表 DML 专用规则 |
| `COPY ... FROM STDIN` | — | 预检查阻断；建议源端 `gs_dump --inserts` |
| `standard_conforming_strings = off` | — | 预检查阻断，避免字符串数据损坏 |

**重要：** MySQL 8.0.13+ 才具有此工具使用的函数索引形式；目标 MySQL 版本及字符序必须核实。部分 CHECK 约束应以 MySQL 8.0.16+ 为基准验证执行行为。只返回 `READY_FOR_TESTING` 的含义是静态规则未发现阻断，不能替代目标数据库实际执行验证。

## 三、这次修订过的说明文字

- 原“SMALLINT → SMALLINT，整数”改为“16 位有符号整数，勿改为浮点数”，减少误选 `REAL` 等类型的风险。
- 原“TIMESTAMP → DATETIME，无时区时使用”改为“无时区日期时间；不自动迁移时区”。
- 原“TEXT → LONGTEXT，扩大文本上限”改为“长文本；确认字符集和存储容量”。
- 原“JSONB → JSON，存储表示可能不同”改为“JSONB 规范化与 JSON 语义可能不同”。
- 原“BYTEA → LONGBLOB，二进制字面量需转换”进一步明确具体使用方式及非支持格式的阻断规则。
- 页面提示调整为“无法安全处理的语法会记录在诊断报告中”，避免被理解为工具能够自动修复所有方言差异。

## 四、自动化验证

```powershell
python -m unittest discover -s tests -v
```

本次重点覆盖 `COMMENT ON`、函数索引、无时区时间、整数与 DECIMAL 精度、JSONB、BYTEA、PG 单引号 / E 字符串、未知 PG 运算符、危险会话设置以及文件流式转换。

除此之外，使用之前 MySQL → OpenGauss 生成的整库文件进行了**静态反向转换**，确保已有原始导出规则未因新增处理器出现阻断。请在目标 MySQL 8.0 测试数据库执行并核验数据行数、字符集、唯一约束及备注。

## 五、在 GitHub 网页更新

1. 下载更新补丁并解压。
2. 进入仓库对应目录，使用网页编辑或 Upload files 更新同名文件；新增 `sql_converter/pg_compat.py`、`tests/test_opengauss_reverse.py` 等新文件。
3. 注意：无需重新上传 `.github`，原有工作流会在 Python 文件变更后触发。
4. 提交信息：`feat: 优化界面并增强 OpenGauss 反向转换规则`。
5. 在 Actions 查看测试及 EXE 构建结果，再下载新的构建产物。

**源码压缩包不包含任何真实业务 SQL 或账户数据。** 真实数据库文件不要上传到公开仓库。
