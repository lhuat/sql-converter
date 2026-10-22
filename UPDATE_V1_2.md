# SQLConverter v1.2 升级说明（500 MiB）

本次增加本地 SQL 文件 **500 MiB** 上限，并修复原版一次性读取整份 SQL 导致的大文件内存压力。

## 网页版 GitHub 更新（不需要 Python）

1. 下载 `sql-converter-v1.2-patch.zip` 并解压。
2. 打开仓库 `https://github.com/lhuat/sql-converter`。
3. 更新下列文件；`sql_converter/streaming.py` 和 `tests/test_large_file.py` 是**新增文件**。
   - `sql_converter/engine.py`
   - `sql_converter/scanner.py`
   - `sql_converter/gui.py`
   - `sql_converter/streaming.py`（新增）
   - `tests/test_large_file.py`（新增）
   - `README.md`
   - `UPDATE_V1_2.md`
4. GitHub 网页可以通过 `Add file → Upload files` 上传/替换文件。注意保留子目录路径，不要额外嵌套 `sql-converter/`。如果浏览器上传目录不成功，可在对应目录点击 `Add file → Create new file`，单独创建新文件后粘贴其内容。
5. 使用中文提交信息：`feat: SQL转换支持500MiB并优化大文件流式处理`。
6. 点击仓库的 `Actions → Build Windows EXE`，确认测试通过并下载 `SQLConverter-Windows-x64`。
7. 解压 `SQLConverter.exe` 覆盖旧 EXE；运行新版并检查输入框提示为“最多 500 MiB”。

> `.github/workflows/build-windows.yml` **本次没有修改，无需重新上传隐藏目录**。

## 边界说明

- 最大源文件大小是 500 MiB，超过 524,288,000 字节拒绝。程序**没有上传服务器**，是本地选择文件。
- 程序逐块扫描并逐条转换；32 MiB 以上的单条 SQL 会成为 `STATEMENT_TOO_LARGE` 阻断项。建议把单条超长 INSERT 分批导出。
- 输入编码按 UTF-8-SIG / GB18030 检查。
- 未支持语法不会被随意转换：将生成 `_review.sql` 和 JSON 报告，不能把 `_review.sql` 作为完整迁移文件执行。
- 自动化测试覆盖文件上限、流式边界、SQL 字符串和注释、中文编码、错误报告，以及原有的转换规则。
- 不保证 MySQL 与 openGauss 所有 SQL 方言均能互转，仍需测试库验证。
