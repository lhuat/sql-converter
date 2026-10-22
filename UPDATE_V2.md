# v2 更新说明 | 整库 SQL 支持与格式优化

本次使用 `iems_cloud(1).sql`（63 表、101 INSERT、55 UNIQUE、108 普通索引、34 CHECK）进行转换回归。

## 修复

1. 支持多种 `CASE WHEN ... ELSE NULL` 唯一表达式索引（多条件 AND、列值、数字常量、LOWER(TRIM(...))）。
2. 支持 `_utf8mb4'...'` 等字符集字面量前缀，尤其是 CHECK 和条件唯一索引中的表达式。
3. 根据 MySQL 默认字符串转义模式安全转换 `\\r`、`\\n`、`\\t`、`\\\\`、`\\'` 等值为 openGauss `E'...'`；长日志和 JSON 不再因为正常换行被阻断。
4. 对 NUL、Ctrl+Z 字符采取阻断；如果检测到 NO_BACKSLASH_ESCAPES 模式则阻断并要求另行处理，避免静默损坏数据。
5. 正常清理 MySQL 表级存储选项计入 INFO，不再当作成批的 WARNING；重复风险在 GUI 汇总显示。
6. 对表达式唯一索引增加 WARNING，提示目标兼容模式与唯一性语义需确认。
7. 文件 500 MiB 限制、单语句 32 MiB 安全阈值、输出文件名自定义、进度和 _review 机制维持不变。

## 注意

- 本版是**整库 MySQL → openGauss 静态转换增强**；不承诺所有 SQL 方言、版本及 openGauss 配置均可无错执行。
- 目标 openGauss 版本和 sql_compatibility 未提供，因此尚不能宣称目标库执行验证通过。请按 docs/SQL_RULE_MATRIX.md 验证。
- 反向 openGauss → MySQL 基础处理器还没有实现全量 COMMENT ON COLUMN、E 文本常量的重建，请勿把复杂转换结果反向转换后视为完整脚本。
- 不要把原始业务 SQL 或包含账号/日志的数据提交到**公开** GitHub 仓库。

## GitHub 网页更新

- 下载完整 v2 源码或 v2 补丁并解压。
- 直接进入仓库的 `sql_converter` 文件夹，更新 `engine.py`、新增 `mysql_compat.py`；同时更新 `gui.py`、`streaming.py`。
- 进入 `tests` 文件夹，更新 `test_engine.py`、新增 `test_mysql_compat.py`。
- 仓库根目录更新 `README.md`、`.gitignore`、`UPDATE_V2.md`；新增 `docs/SQL_RULE_MATRIX.md`。
- 保留原有 `.github/workflows/build-windows.yml`，不用重新上传隐藏目录；直接提交到 main 后 GitHub Actions 自动构建。
