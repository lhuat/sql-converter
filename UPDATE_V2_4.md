# SQLConverter v2.4 · 修复真实整库误报与批量 INSERT 性能

## 根因确认

用户提供的完整 `iems_cloud.zip` 中 `iems_cloud.sql` 的实际大小为 **281,786,343 字节（268.73 MiB）**，共有 **674,324 个物理行**。第 613 行为 `DROP TABLE IF EXISTS sys_dict_data`，不是超过 32 MiB 的 SQL。

第 639 行的 Navicat 表达式唯一索引出现 `_utf8mb4\\'\\'`，现有流式词法分析器将第一个撇号误认为字符串起始，并错误吞并了其后的 SQL 分号，最终给出以第 613 行附近为起点的 `STATEMENT_TOO_LARGE` 阻断。**这不是用户的 INSERT 记录数量过大造成的。**

### 修改措施

1. `sql_converter/scanner.py`、`streaming.py`：两种扫描器均识别极窄的 `_utf8mb4\\'\\'`、`_utf8mb3\\'\\'`、`_utf8\\'\\'` 成对空字符串形式；支持跨读取块边界，避免错误吞并独立 SQL。此例外仅在 MySQL 模式生效；普通的非法反斜杠引号仍不能直接通过。
2. `sql_converter/mysql_compat.py`、`engine.py`：**仅在 CREATE TABLE 的 CASE WHEN 唯一/普通索引表达式中**，将 Navicat 的该种空字符串异常转义修正为标准的 `_utf8mb4''` 写法；保留 `NAVICAT_ESCAPED_EMPTY_LITERAL` 警告和实际源文件行号。不会对 INSERT 数据、JSON 字段、日志或普通字符串执行全局替换。
3. `engine.py`：普通单行/短批次 `INSERT ... VALUES` 新增 **严格语法校验的快速路径**。仅接受标准 SQL 字符串（无需反斜杠转义）、数值、NULL、TRUE、FALSE，超长/特殊/二进制/函数形式仍使用既有完整转换及阻断规则。正反两个方向均支持此快速路径。
4. 新增 `tests/test_navicat_large_dump.py` 回归测试，兼顾 MySQL → OpenGauss、OpenGauss → MySQL、跨块读取、普通数据无损与危险语法回退。

## 使用完整真实文件的静态验证（仅在本地）

- 文件大小：**268.73 MiB**；物理行数：**674,324**。
- 流式 SQL 语句：**672,390**，0 条超大语句误判、0 条未结束语句；最长解析语句约 **60 KiB**；扫描耗时约 **15.4 秒**。
- MySQL → OpenGauss 实际完整转换：**672,390 条总语句；672,387 条成功转换；3 条源端会话设置跳过；0 条 BLOCKER**。耗时 **34.4 秒**，输入吞吐量 **7.812 MiB/s**（本次 Linux 运行环境，非 Windows EXE 测试）。
- 转换报告仍保留字段字符序、表达式唯一索引、导出会话约束等真实兼容性警告。
- 真实文件前 **20,000 条完整 SQL** 进行反向验证：先 MySQL → OpenGauss，再 OpenGauss → MySQL，两个方向均 0 条 BLOCKER；**19,928 条 INSERT** 前后数量一致。
- 完整 268.73 MiB SQL 的 **反向端到端转换未完成**，不宣称逆向整库已全部验证。未在真实目标数据库上执行 SQL。
- Python unittest：**103 项，其中 101 项通过，2 项 GUI 测试因本地没有 PySide6 而跳过**。

## 保留的安全边界

- 整个 SQL 文件上限仍是 **500 MiB**。
- 真正的**单条 SQL**（包括极长多元组 INSERT）若超过 **32 MiB**，仍按原规则阻断；本次修复的是词法误判，不是无条件支持任意大的单条语句。
- 对 SQL 无法证明等价的排序规则、表达式索引、二进制、时区等不会静默忽略。
- `READY_FOR_TESTING` 表示静态转换没有已知阻断，**不表示已在数据库导入成功**。正式迁移前应使用对应版本的空测试库验证。

## GitHub 网页更新

将更新包中的以下内容上传或编辑到仓库对应路径（注意保留目录）：

- `sql_converter/scanner.py`
- `sql_converter/streaming.py`
- `sql_converter/mysql_compat.py`
- `sql_converter/engine.py`
- `tests/test_navicat_large_dump.py`（新增）
- `README.md`
- `UPDATE_V2_4.md`（新增）

`.github/workflows/build-windows.yml` **无需修改**，提交 Python 文件后即可自动触发测试与 Windows EXE 构建。

**隐私说明**：源码包、补丁包及测试均未包含用户上传的真实 SQL、账户信息或业务数据。不要把真实整库 SQL 上传到公开 GitHub 仓库。
