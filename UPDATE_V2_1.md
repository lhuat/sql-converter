# SQLConverter v2.1 性能优化更新

## 适用版本与安装

适用于当前 `lhuat/sql-converter` 仓库的 v2 代码。从 `sql-converter-v2.1-performance-patch.zip` 解压，按原目录替换文件；新增 `tests/test_performance_regression.py`、`benchmarks/benchmark_conversion.py` 和本说明文件。现有 `.github/workflows/build-windows.yml` 无需修改；提交至 `main` 后，GitHub Actions 将自动运行测试并构建 Windows EXE。

仓库仍只能通过只读 GitHub 连接访问；**本补丁没有自动提交到 GitHub**。不要把实际业务 SQL 或报告上传到公开仓库。

## 本次修改

1. 语法事件驱动的 SQL 流式扫描，减少对大段 INSERT 文本逐字符遍历。
2. 优化 MySQL 字面量、标识符、掩码检查；不更改转换出的 SQL 语义。
3. 合并编码验证和危险语法指令扫描，减少整文件读取次数。
4. 减少 UI 进度更新次数，大文件任务仍运行在后台线程。
5. GUI 增加取消按钮，取消时清理临时文件。
6. 报告展示 `metrics.input_bytes`、`metrics.elapsed_seconds`、`metrics.average_mib_per_second`。
7. 新增独立的合成数据性能测试与边界、取消回归测试。

## 测试结果

- 68 项 Python 单元测试通过。
- `iems_cloud(1).sql`：生成的 SQL 与 v2 完全一致，SHA-256 为 `28e48086e9e3ab98f1f42668c2aa51ae102da09f7a5945b0969b0d209448911e`。
- 同一环境：约 3.14 MiB 实际文件，v2 中位数 3.81s，v2.1 中位数 1.15s；20.1 MiB 合成文件分别约 25.01s 和 7.67s。仅供参考，不代表 Windows 真实速度。
- 文件上限仍为 **500 MiB**，单条 SQL 安全上限仍为 **32 MiB**，超过时报告 `STATEMENT_TOO_LARGE`。

## 运行方式

无需修改原有 `requirements.txt` 或 GitHub Actions 工作流。完整源码 ZIP 也可直接上传到一个新仓库，注意保留 `.github/workflows/build-windows.yml`。

## 限制

本次侧重吞吐性能和可取消性，不扩大 SQL 方言支持范围，不承诺所有目标 openGauss 版本均可零错误导入。正式使用前应先在测试库检查字符序、索引约束及导入结果。
