# SQL Converter | MySQL ↔ openGauss 桌面转换工具

一个无需数据库连接、无需远程后端的 **Windows 离线 SQL 文件转换工具**。桌面界面使用 PySide6，SQL 转换核心仅依赖 Python 标准库。支持 MySQL 与 openGauss 的**保守子集**转换，无法安全转换的语句会在报告中明确标记，不能视为完整迁移成功。

> ⚠️ **请勿将转换结果未经测试就直接导入生产数据库。** `READY_FOR_TESTING` 表示本工具没有检测到已知阻断项，**不表示已通过目标数据库执行验证**。数据库版本、openGauss 兼容模式、Dolphin 插件、字符集、权限以及实际数据可能影响结果。

## 功能

- MySQL → openGauss / openGauss → MySQL 双向转换。
- 本地 `.sql` 文件选择、源文件编码选项、输出目录选择。
- `CREATE TABLE`、`DROP TABLE`、常规索引、联合索引、联合唯一约束、简单 `ALTER TABLE`、`INSERT ... VALUES`、简单 `UPDATE` 与 `DELETE`。
- 旧版 MySQL 整数显示宽度、自增列、常见数据类型及参数 `VARCHAR(n)`、`DECIMAL(p,s)`、`DATETIME(p)`。
- 数据类型映射支持不区分大小写的模糊搜索、下拉选择、手动输入，以及保存到本机用户配置目录。
- `.sql` 输出及 `*_report.json` 报告：按行号列出 INFO、WARNING、BLOCKER；阻断时输出命名为 `*_review.sql`。
- 原有输出文件不覆盖，自动追加 `_2`、`_3` 等编号。
- GitHub Actions 自动运行单元测试并打包 Windows EXE。

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

阻断意味着部分语句**没有被输出成可执行 SQL**，必须人工处理；不能直接当作完整迁移脚本运行。

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
sql_converter/mappings.py    # 类型模板、默认映射、自定义配置
samples/                     # 双向转换示例
tests/                      # 标准库 unittest 测试
.github/workflows/           # Windows CI 与 PyInstaller EXE 构建
```

## 当前明确的限制（很重要）

1. **没有承诺完整 SQL 方言兼容**：复杂视图、存储过程、触发器、分区表、特殊函数、COPY FROM STDIN、MySQL DELIMITER、函数索引、前缀索引和复杂 upsert 等会被阻断或需要后续新增规则。
2. 不执行任何 SQL、不验证目标库真实版本/运行时配置；openGauss 兼容模式差异需在测试库人工检查。
3. `INSERT` 只覆盖标准 `INSERT INTO ... VALUES` 形式；可能改变含反斜杠转义、十六进制、二进制等特殊值语义的语句会被阻断，避免静默数据污染。
4. 单文件最大 **80 MiB**；若导出库非常大，建议分批导出并按依赖顺序导入。
5. 编码自动尝试 UTF-8 和 GB18030（也可以手动选择）；建议导出 UTF-8 格式。
6. 默认输出 UTF-8 BOM 编码以便 Windows 常见编辑器识别中文；若目标管理工具对 BOM 不兼容，手动用 UTF-8（无 BOM）重新保存文件。
7. 映射允许自定义，但目标类型还会经过基础类型白名单校验，不允许完全任意 SQL 字符串作为类型。
8. 当前没有签名证书，EXE 可能触发 SmartScreen 提示；GitHub Actions 仅构建程序，不会绕过系统安全机制。

## 如何扩展未支持语法

参见 [RULES.md](RULES.md)。收到报告中的 `code`、`line` 和原 SQL 后，按规则接口添加处理器并补单元测试，之后重新触发 GitHub Actions 构建即可。**不要通过简单的全局正则替换原始 SQL 来绕过语法解析。**

## 隐私与安全

所有 SQL 内容只在当前计算机处理，不上传服务器；程序不需要数据库连接、账号或密码。注意转换生成的报告会包含**部分原始 SQL 片段**，其中可能有业务数据，请像管理 SQL 备份一样管理报告文件。GitHub 仓库只存放示例数据，**不要将真实业务 SQL、密钥、客户数据提交到公开仓库**。
