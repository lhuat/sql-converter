# MySQL ↔ openGauss SQL 文件转换规则与验证清单

本项目是**本地离线 SQL 文件转换器**，不会连接数据库、不会自动执行 SQL。

- `READY_FOR_TESTING`：已知规则中 0 条阻断，**不是**目标数据库实测成功。
- `MANUAL_REVIEW_REQUIRED`：至少一条语句未能安全转换；SQL 文件自动添加 `_review`，**不可当作完整迁移文件执行**。
- `INFO`：预期的规则处理，如删除源数据库专属存储引擎参数。
- `WARNING`：SQL 语法虽转换了，但存在不能从导出文件证明的目标库语义/运行环境差异。
- `BLOCKER`：转换可能丢失数据或改变约束，拒绝生成貌似有效的 SQL。

## 1. 当前样本（私有 `iems_cloud(1).sql`）解析结果

- 63 个 `CREATE TABLE`；101 条 `INSERT INTO ... VALUES`；3 条会话设置；总计 167 条 SQL 语句。
- 108 个普通索引、55 个唯一索引（其中 17 个带 CASE WHEN 的条件表达式唯一索引）、34 个表级 CHECK 约束。
- 字段以 bigint、varchar、smallint、datetime、int、timestamp、text、decimal、json 为主，也涉及 longtext、longblob。
- 有中文/Unicode、多个租户的业务数据、复杂日志、Three.js 场景 JSON、长字符串和 MySQL 反斜杠转义。
- 当前版本静态扫描 0 BLOCKER，所有 101 条 INSERT 的字符串转义与列标识符处理已经逐条核对；尚未连接 openGauss 执行验证。

注意：以上统计对应本次提供的这个具体导出文件，不代表对其他 MySQL dump 同样无需人工处理。

## 2. MySQL → openGauss 处理策略

| 类别 | 示例/变化 | 处理状态与说明 |
|---|---|---|
| 表名与标识符 | 反引号 `a` → 双引号 `"a"` | 仅转换 SQL 标识符区域，保留字符串内的反引号 |
| 建表基础 | `CREATE TABLE`、主键 | 生成规范缩进，保留主键和顺序 |
| `ENGINE`/`ROW_FORMAT` | MySQL 独有的存储引擎参数 | 安全移除并记录 INFO |
| 字段/表 COMMENT | `COMMENT '名称'` | 生成 `COMMENT ON COLUMN/TABLE` |
| 普通/联合索引 | `KEY name (a,b)` | 转为 `CREATE INDEX`，不改变列顺序 |
| 普通联合唯一索引 | `UNIQUE KEY(a,b)` | 转为具名 UNIQUE 约束 |
| CASE 条件唯一索引 | `CASE WHEN del_flag=0 THEN ... ELSE NULL END` | 保留为独立 UNIQUE 表达式索引；目标版本/兼容模式需检查 |
| CHECK | `CHECK ((flag IN (0,1)))` | 保留 CHECK，统一 UTF8 字面量标识 |
| Unicode 字面量 | `_utf8mb4'abc'` | UTF8 兼容 introducer 移除，保留字符串值 |
| MySQL 文本转义 | `\\r`、`\\n`、`\\t`、`\\\\`、`\\'` | 按 MySQL 默认 SQL_MODE 解析后，安全写成 openGauss `E'...'` |
| 控制字符 | `\\0`、`\\Z` | 阻断：不能无损写入文本字段 |
| SQL 字符串中英文、JSON | 中文、`{\"a\":1}`、长堆栈 | 不拆字符串、不把内容识别为 SQL 指令；保证内容逐条一致 |
| 数据类型 | `datetime`、`tinyint`、`decimal(p,s)`、`bigint unsigned` | 按内置映射，可搜索下拉并调整目标类型 |
| 插入数据 | 多行 `INSERT INTO ... VALUES (...),(...);` | 保持每列的顺序、数据和 NULL，原脚本中的多行格式不随意压缩 |
| SQL 文件中的 SET | `SET NAMES`, `FOREIGN_KEY_CHECKS` | 忽略已知源库会话设置，约束校验关闭需 WARNING |
| 字段字符序 | `utf8mb4_bin`, `utf8mb4_general_ci` | 可移除专有写法；WARNING：目标库字符比较/排序规则必须独立确认 |
| 表级字符序 | `COLLATE=utf8mb4_bin` | 抑制源库专用尾部参数，字符序语义留风险提示 |
| `NO_BACKSLASH_ESCAPES` | `SET SQL_MODE=...` | 预先阻断：需指定不同词法模式才能安全解析 |
| 外键/FK | `FOREIGN KEY` | 源 dump 若依赖 `FOREIGN_KEY_CHECKS=0`，必须先排序或延后加约束；当前暂阻断，绝不静默放行 |
| 极长 SQL | 500 MiB 整库 | 整库流式读取，每条语句最多 32 MiB；再大需分批导出 |
| 复杂 DDL/DML | 程序/触发器/分区表、MERGE/UPSERT | 当前未保证，生成具体阻断报告，不伪装成功 |

**条件唯一索引不能被替换为普通唯一约束。** 它用于实现 `del_flag=0` 数据才参与唯一性限制。例如 `UNIQUE (tenant_id, code, CASE WHEN del_flag=0 THEN 1 ELSE NULL END)`，如误拆分或删除表达式会改变业务规则。

## 3. openGauss → MySQL 现阶段能力与限制

- 支持已验证的简单 CREATE TABLE、基础字段类型、BIGSERIAL/SERIAL、简单索引、简单 INSERT/UPDATE/DELETE。
- openGauss 的 `COMMENT ON COLUMN`、复杂表达式索引、`COPY FROM STDIN`、存储过程/触发器及 `E'...'` 等反向处理，需要额外规则或结构状态，不能声称可完全逆向还原。
- 在反向转换时不应强行抹掉 COMMENT、索引、检查约束等数据库对象，未支持应明确标记 `BLOCKER`。

## 4. 文件格式与容错

- 输入编码自动检测 UTF-8（包含 BOM）/GB18030，可手动切换。
- 输出 `.sql` 默认文件名是 `<原文件名>_opengauss.sql` 或 `_mysql.sql`；允许直接修改。阻断自动加 `_review`。
- 输出与报告先写入临时文件，完成后再发布；不覆盖已有结果，同名添加 `_2`、`_3`。
- 输出包含转换说明、空行分隔语句，CREATE TABLE 字段一行一列，独立 INDEX 与 COMMENT 行排列。
- 文件最大 500 MiB，文件大小超限直接拒绝；单条 SQL 最大 32 MiB，拒绝无提示的截断。
- 不在 GUI 主窗口逐条铺满重复 INFO/WARNING：分组汇总计数；阻断列出行号，完整内容在 JSON 报告中。
- 保持导入源数据、字面量、时间戳/小数精度和长 ID；不能把数据库中的 BIGINT ID 转成 Python 浮点数。

## 5. 目标数据库实测清单（正式库导入前必须）

1. 建立**空的测试库**，确认 openGauss 版本和兼容模式（若使用表达式索引，重点核对 B 模式及相应支持）。
2. 确认数据库 `server_encoding`、schema/table/column `COLLATE`、大小写是否敏感和唯一约束语义；本 SQL 使用大量 `utf8mb4_bin`。
3. 在 Navicat/兼容管理工具上先导入测试库；不可在生产库直接试跑。
4. 验证表数量、列名/类型/非空/默认值、主键、普通索引、唯一索引、CHECK 和表字段备注。
5. 检查每张表的行数，并抽样比对 JSON、日志堆栈、中文、日期、十进制小数、长 BIGINT。
6. 检查带逻辑删除的唯一索引：`del_flag=0` 不允许重复，`del_flag=1` 可保留多版本的预期是否满足。
7. 检查应用端 JDBC/MyBatis 查询/更新 SQL、索引执行计划、分页、时间与默认值行为。
8. 发现目标库执行错误时保留 SQL 和报错行号，修复对应转换规则并增加测试回归。

**不建议发布至公共 GitHub 仓库：** 导出的整库 SQL 含有账户资料、密码哈希、业务日志和其他敏感数据。仓库里只保留脱敏合成样例和源码。

## 6. 扩展处理器规则

新增处理器必须遵守：
- 规则首先精确识别来源语法；遇到未知变体主动阻断。
- 不直接对全 SQL 全局替换关键字，要识别字符串、注释、标识符区域。
- 每个处理器必须附常规用例、复杂用例、恶意/错误输入、真实回归脱敏样例。
- 规则输出既要过语法检查，也要检查索引、约束和数据语义。
- 报告 JSON 保留 `line`、`code`、`severity`、`excerpt`、`finding_summary`，方便定位规则缺口。
