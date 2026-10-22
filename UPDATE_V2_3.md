# SQLConverter v2.3 · 桌面界面布局修复

本版本仅调整 **PySide6 桌面 GUI 的排版与文案**，不改动 SQL 方言、转换引擎、类型映射表的实际数据或大文件处理逻辑。

## 修复内容

1. **取消三个 QGroupBox 默认标题**：`文件与转换方向`、`数据类型映射（目标类型可搜索 / 下拉 / 手动修改）`、`转换结果与语法报告` 统一使用 `QFrame` 卡片和独立标题行。标题不再与边框、内容控件共享定位区域，从而避免 Windows DPI 缩放下文字骑线、遮挡的问题。
2. **统一卡片间距**：外层间隔 15px，卡片标题单独保留上下边距；映射表与筛选条件之间保留必要的间隔。
3. **结果区收紧**：进度条与结果卡片之间额外留出 9px；结果区正文行间距 5px，底部内边距 9px，日志框高度 116px。窗口不足时使用最外层滚动区，不挤压标题和日志。
4. **耗时和日志对齐**：耗时/速度文本左侧内容边距 10px，尽量匹配日志编辑框内部文字的起始位置。结果区操作按钮不再占用多余空白。
5. **说明文案调整**：标题副文案改为“无法安全转换的语法将记录在诊断报告中”；空闲提示改为“若报告中出现 BLOCKER，表示存在需要处理的阻断项，不能直接将输出 SQL 作为完整迁移脚本执行”。

## GitHub 上传方法（网页版）

将补丁包中的文件放到 GitHub 仓库的**相同路径**：

- `sql_converter/gui.py`：替换旧文件。
- `tests/test_gui_layout.py`：新增文件。
- `.github/workflows/build-windows.yml`：修改工作流，让 Windows 构建阶段实际安装 PySide6 后运行 GUI 布局冒烟测试。网页若无法上传 `.github` 文件夹，进入原有 `build-windows.yml` 通过铅笔图标替换内容即可。
- `README.md` 和 `UPDATE_V2_3.md`：根目录更新与新增。

完成提交后，进入 Actions → Build Windows EXE，下载构建成功的 EXE。

## 验证范围

- 当前无 PySide6 的开发环境：`python -m unittest discover -s tests -v` **94 项转换核心测试通过**；额外的 **2 项 GUI 布局测试因未安装 PySide6 被跳过**。
- 已通过 `py_compile` 检查 GUI Python 语法。
- `.github/workflows/build-windows.yml` 已增加 `QT_QPA_PLATFORM=offscreen` 的布局冒烟测试；在真实 Windows Runner 构建之后才能确认 Qt 实际渲染行为。
- **不会**因为本次 UI 修复改变 SQL 转换输出。
