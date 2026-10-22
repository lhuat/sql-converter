# SQL Converter | MySQL ↔ OpenGauss 桌面转换工具

一个无需数据库连接、无需远程后端的 **Windows 离线 SQL 文件转换工具**。桌面界面使用 PySide6，SQL 转换核心仅依赖 Python 标准库。支持 MySQL 与 OpenGauss 的**保守子集**转换，无法安全转换的语句会在报告中明确标记，不能视为完整迁移成功。

> ⚠️ **请勿将转换结果未经测试就直接导入生产数据库。** `READY_FOR_TESTING` 表示本工具没有检测到已知阻断项，**不表示已通过目标数据库执行验证**。数据库版本、openGauss 兼容模式、Dolphin 插件、字符集、权限以及实际数据可能影响结果。

## 功能

- MySQL → openGauss / openGauss → MySQL 双向转换。
- 本地 `.sql` 文件选择、源文件编码选项、输出目录选择；**自动生成输出文件名，允许手动修改**。
- 小尺寸/高缩放屏幕使用滚动布局，映射表格固定行高，避免文字与控件重叠。
- `CREATE TABLE`、`DROP TABLE`、常规索引、联合索引、联合唯一约束、特定 `CASE WHEN` 表达式唯一索引、简单 `ALTER TABLE`、`INSERT ... VALUES`、简单 `UPDATE` 与 `DELETE`。
- 旧版 MySQL 整数显示宽度、自增列、常见数据类型及参数 `VARCHAR(n)`、`DECIMAL(p,s)`、`DATETIME(p)`。
- 数据类型映射支持不区分大小写的模糊搜索、下拉选择、手动输入，以及保存到本机用户配置目录。
- `.sql` 输出及 `*_report.json` 报告：按行号列出 INFO、WARNING、BLOCKER；相同语句的重复排序规则告警会合并，未使用的自定义映射不再产生警告；阻断时输出命名为 `*_review.sql`。
- 原有输出文件不覆盖，自动追加 `_2`、`_3` 等编号。
- GitHub Actions 自动运行单元测试并打包 Windows EXE。
- **文件最大 500 MiB：采用流式解析和增量输出；单条 SQL 的内存安全上限为 32 MiB，支持进度、取消及临时文件清理。**
- **v2.2：采用已确认的统一网格布局、白/灰交替表格和同色下拉框，并增强 OpenGauss → MySQL 的备注、索引、字段精度、转义字符串及 BYTEA 数据转换。**
- **v2.3：修复三个卡片标题与边框重叠，缩小结果区底部留白，并在 Windows 构建中增加 GUI 布局测试。**

## 使用方式（电脑**不需要**安装 Python）

1. 把完整项目上传到 GitHub 仓库的 **根目录**（使仓库根目录能看到 `main.py` 和 `requirements.txt`）。
2. 打开 **Actions → Build Windows EXE → Run workflow**，选择 main，运行。
3. 打开成功的运行记录，在 Artifacts 下载 **SQLConverter-Windows-x64** ZIP。
4. 解压得到 `SQLConverter.exe`，双击运行。程序不会连接远程数据库，也不会上传文件。
5. 选择源 SQL → 指定方向 → 可选修改类型映射 → 开始转换 → 检查 JSON 报告 → 在测试数据库进行实际执行验证。

首次启动 Windows 可能展示未知发布者/安全提示：项目尚未购买代码签名证书，下载后建议核验来源和扫描文件。

## 输出文件举例

| 输入文件 | 方向 | 输出 SQL | 报告 |
| --- | --- | --- | --- |
| `iems_data.sql` | MySQL → openGauss | `iems_data_opengauss.sql` | `iems_data_opengauss_report.json` |
| `iems_data.sql` | openGauss → MySQL | `iems_data_mysql.sql` | `iems_data_mysql_report.json` |
| `iems_data.sql` | 存在阻断 | `iems_data_opengauss_review.sql` | `iems_data_opengauss_report.json` |
| `iems_data.sql` | 自定义名称 `my_backup.sql` | `my_backup.sql` | `my_backup_report.json` |

阻断意味着部分语句**没有被输出成可执行 SQL**，必须人工处理；不能直接当作完整迁移脚本运行。自定义名称如果发生阻断，会自动变为 `my_backup_review.sql`。输出文件已存在时依次追加 `_2`、`_3`，绝不覆盖已有文件。

## 本地开发（仅开发人员需要 Python）

建议 Python 3.12：

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python main.py
python -m unittest discover -s tests -v
```

本地 Windows 打包：

```powershell
python -m pip install pyinstaller==6.16.0
pyinstaller --noconfirm --clean --onefile --windowed --name SQLConverter main.py
```

## 代码结构

```text
main.py                      # 桌面程序入口
sql_converter/gui.py         # PySide6 独立窗口、映射搜索和任务执行
sql_converter/engine.py      # 保守语句处理器 + JSON 报告
sql_converter/scanner.py     # SQL 分句、括号识别、标识符引号转换
sql_converter/streaming.py   # 大文件流式解析、500 MiB 限制、文件编码检测
sql_converter/mappings.py    # 类型模板、默认映射、自定义配置
sql_converter/pg_compat.py   # OpenGauss 字符串转义和受限 CAST 转换
samples/                     # 双向转换示例
tests/                      # 标准库 unittest 测试
.github/workflows/           # Windows CI 与 PyInstaller EXE 构建
```

## 当前明确的限制（很重要）

1. **没有承诺完整 SQL 方言兼容**：复杂视图、存储过程、触发器、分区表、特殊函数、COPY FROM STDIN、MySQL DELIMITER、未覆盖的函数/表达式索引、前缀索引和复杂 upsert 等会被阻断或需要后续新增规则。
2. 不执行任何 SQL、不验证目标库真实版本/运行时配置；openGauss 兼容模式差异需在测试库人工检查。
3. `INSERT` 只覆盖标准 `INSERT INTO ... VALUES` 形式；可能改变含反斜杠转义、十六进制、二进制等特殊值语义的语句会被阻断，避免静默数据污染。
4. 单文件最大 **500 MiB（524,288,000 字节）**；超过上限时会明确提示。大型文件采用分块读取、逐条转换和临时文件写出，避免整文件进入内存。**单条 SQL 超过 32 MiB（按 UTF-8 字节数计）将标记为 `STATEMENT_TOO_LARGE` 阻断**，需分批导出或拆分超长的扩展 INSERT；不能理解为任意 500 MiB 的单条语句都能完成转换。
5. 编码自动尝试 UTF-8 和 GB18030（也可以手动选择）；建议导出 UTF-8 格式。
6. 默认输出 UTF-8 BOM 编码以便 Windows 常见编辑器识别中文；若目标管理工具对 BOM 不兼容，手动用 UTF-8（无 BOM）重新保存文件。
7. 映射允许自定义，但目标类型还会经过基础类型白名单校验，不允许完全任意 SQL 字符串作为类型。
8. 当前没有签名证书，EXE 可能触发 SmartScreen 提示；GitHub Actions 仅构建程序，不会绕过系统安全机制。

## 如何扩展未支持语法

参见 [RULES.md](RULES.md)。收到报告中的 `code`、`line` 和原 SQL 后，按规则接口添加处理器并补单元测试，之后重新触发 GitHub Actions 构建即可。**不要通过简单的全局正则替换原始 SQL 来绕过语法解析。**

## 隐私与安全

所有 SQL 内容只在当前计算机处理，不上传服务器；程序不需要数据库连接、账号或密码。注意转换生成的报告会包含**部分原始 SQL 片段**，其中可能有业务数据，请像管理 SQL 备份一样管理报告文件。GitHub 仓库只存放示例数据，**不要将真实业务 SQL、密钥、客户数据提交到公开仓库**。

## v1.1 修复（Navicat MySQL 8.0 导出回归）

- 修复普通字符串 `'b'` 被误判为 `B'101'` 二进制字面量；真正二进制字面量仍会安全阻断。
- 为 MySQL 字段级 `CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci` 增加处理，**仍会提示排序规则和大小写唯一性可能不同**。
- 支持受约束的 `CASE WHEN (del_flag = 0) THEN 1 ELSE NULL END` 联合唯一表达式索引，避免错误删除逻辑删除约束。
- 正确转换 MySQL 表级 `CHARACTER SET = utf8mb4`、主键 `USING BTREE` 和保留字段/表注释。
- 合并重复诊断，仅报告**实际使用到**的自定义类型映射；对于 `FOREIGN_KEY_CHECKS = 0` 仍保留约束警告。
- 修改 PySide6 窗口滚动和映射表格行高；增加默认输出文件名编辑框。

**真实回归结果**：用户提供的单表 SQL 共 17 条语句，14 条转换、3 条会话设置跳过、0 条阻断、12 条 INSERT 保留；尚未对目标 openGauss 数据库做实机执行验证。

## v1.2 文件处理增强（最大 500 MiB）

- GUI 文件选择和实际转换入口均校验源 SQL 文件是否超过 500 MiB。
- 先分块验证编码和检查 `DELIMITER` / `COPY` 客户端指令；危险导出模式整份阻断。
- 流式按 SQL 语句读取、转换并持续写入临时文件，不再对整个 SQL 执行 `read_bytes()` / `split_sql()`。
- 转换失败自动删除临时文件，结果和报告不会覆盖历史文件；源文件在转换期间修改也会报错。
- 针对 32 MiB 以上的单条 SQL 采取阻断策略，不会偷偷截断；复杂超长 INSERT 建议控制导出时的批量大小。
- 对特别多的语法问题，JSON 最多保留 5,000 条诊断详情，`omitted_findings` 统计其余条数；GUI 只预览前 200 条。
- 工作流已有 `.py` 路径触发条件，新增 `sql_converter/streaming.py` 与 `tests/test_large_file.py` 后会自动执行测试并重新打包。
- 已用实际 `td_project.sql` 验证：14 条处理、3 条会话设置跳过、0 阻断、12 条 INSERT 保留。**仍须在目标 openGauss 测试数据库验证 SQL。**

## v2 整库回归与安全转义升级

- 更新 MySQL 转换：保留更复杂的条件唯一索引（CASE WHEN、AND、多列及 LOWER/TRIM），不将逻辑删除约束丢弃。
- 正确解析 MySQL 字符串转义（`\\r`、`\\n`、`\\t`、`\\\\`、`\\'`），以 openGauss `E'...'` 表达，允许包含多行日志与 JSON 的真实数据导出。
- `_utf8mb4` 字面量前缀在 CHECK/索引条件中标准化；未知字符串模式、无法无损存储的控制字节均阻断。
- 报告按问题类别提供统计；GUI 汇总同类问题而不是不断重复相同提示。
- **用户提供的整库样本**：63 CREATE TABLE、101 INSERT、55 UNIQUE、108 普通索引、34 CHECK；静态转换 0 BLOCKER，101 条 INSERT 逐条一致性比对通过。
- 完整适配列表、尚不支持的 SQL、Navicat/openGauss 目标库执行验证清单见 [docs/SQL_RULE_MATRIX.md](docs/SQL_RULE_MATRIX.md)。
- GitHub 仓库是公开的，严禁上传未经脱敏的业务 SQL；整库样本、转换结果和 JSON 报告仅作为用户本地文件交付，不包含在源码 ZIP 中。

## v2.1 大文件性能与可取消转换

本版在不改变现有支持范围的情况下，重点优化 SQL 转换性能：

- SQL 流式分句器改为**按语法事件跳转**，普通文本无需由 Python 逐字符检查。
- MySQL 字面量转换、标识符转换和 SQL 安全扫描减少重复字符串遍历与正则检查。
- **编码验证与危险导出指令检查合并成一次磁盘扫描**，不牺牲严格解码检查。
- 文件读取块调整为 1 MiB，转换进度回调最多约 100 次，减少 GUI 信号开销。
- PySide6 窗口增加**取消转换**操作。取消时后台工作线程安全退出、临时文件清理，原始 SQL 不变。
- 结果区与 JSON 报告增加**实际耗时**和**平均 MiB/s**，供不同电脑比较转换性能。
- `benchmarks/benchmark_conversion.py` 可以生成完全虚构的数据集进行本地离线基准测试。

**性能实测**（同一 Linux 运行环境，非 Windows EXE，数字仅供参考）：

| 测试 | v2 | v2.1 |
|---|---:|---:|
| 用户提供的约 3.14 MiB SQL，3 次运行中位数 | 3.81 秒 | 1.15 秒 |
| 20.1 MiB 合成 SQL，1 次运行 | 25.01 秒 | 7.67 秒 |

真实整库样本的输出 SQL **逐字节相同**（SHA-256 一致），且 v2.1 的 68 项单元测试通过。
速度因文件中 INSERT/DDL 比例、字符串长度、磁盘、CPU、杀毒扫描、目标系统负载等不同而变化，**不能按小文件测速直接推断 500 MiB 的耗时**。

运行基准测试（需开发机有 Python 3.12+，普通 EXE 用户不需要）：

```powershell
python -m benchmarks.benchmark_conversion --size-mib 20
```

**仍需注意**：500 MiB 是整个 SQL 文件大小上限，单条 SQL **32 MiB** 的安全阈值仍然存在。包含单条超长扩展 INSERT 的 SQL，应重新导出为较小批次；为了性能或“零报错”不应静默拆分有事务或函数副作用的单条语句。`_review.sql` 包含阻断标记，不能直接作为完整迁移脚本执行。自动化测试不替代目标 openGauss / MySQL 的真实导入验证。

## v2.2 注意事项

请阅读 [`UPDATE_V2_2.md`](UPDATE_V2_2.md)，尤其注意目标 **MySQL 8.0.13+ 函数索引**、MySQL 8.0.16+ CHECK 约束行为、OpenGauss BYTEA 数据编码、JSONB 语义、无界 NUMERIC 和时区类型。工具对无法保证数据/约束语义的差异阻断，不以“无错误”替代真实测试验证。


## v2.3 GUI 布局修复

参见 [UPDATE_V2_3.md](UPDATE_V2_3.md)。本次只调整界面布局与文字，不修改 SQL 解析/转换引擎。
