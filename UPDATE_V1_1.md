# v1.1 网页版 GitHub 更新说明

本次仅需更新 **5 个现有文件**，不需要重新添加 `.github` 文件夹，不用安装 Python。

需要更新的文件：

- `sql_converter/engine.py`
- `sql_converter/gui.py`
- `tests/test_engine.py`
- `README.md`
- `RULES.md`

## 网页更新

1. 下载 `sql-converter-v1.1-patch.zip`，右键“全部解压”。
2. 进入 GitHub 仓库 `https://github.com/lhuat/sql-converter`，点击 **Add file → Upload files**。
3. 把解压后的 **5 个文件/文件夹**拖到上传区，保持原来的目录结构（`sql_converter/`、`tests/` 位于仓库根目录）。不要上传整个压缩包，也不要额外套一层文件夹。
4. 检查待提交文件路径，提交说明填写 `fix: 修复 SQL 转换误报及桌面布局，支持自定义输出文件名`。
5. 保存到 `main`，随后进入 **Actions → Build Windows EXE**。工作流由 `.py` 文件更新自动触发；也可以手动 Run workflow。
6. 等工作流绿色成功，再到 **Artifacts** 下载 `SQLConverter-Windows-x64`。旧版 EXE 不会自动更新，请下载新构建的 EXE。

若网页版无法批量更新已有同名文件，可逐个点击 GitHub 文件页面的编辑铅笔图标，将新文件文本复制进去并提交，**无需改动原有 GitHub Actions 工作流**。

## 当前验证范围

- Python 转换核心通过 41 项单元测试。
- 在用户提供的 `td_project.sql` 上完成转换，17 条源 SQL（14 转换、3 跳过、0 阻断），12 条 INSERT 内容完整保留。
- PySide6 未安装在本次处理环境，因此桌面 GUI 已通过 Python 语法编译检查，**但新布局尚未完成 Windows 实际启动截图验证**，需依靠 GitHub Actions 生成 EXE 后在 Windows 中查看。
- 未连接实际 openGauss 数据库做 SQL 真实执行验证。
