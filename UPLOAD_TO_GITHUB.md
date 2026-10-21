# 将项目放入 https://github.com/lhuat/sql-converter

当前仓库连接只有读取权限，不能由 ChatGPT 直接写入。因此先提供完整源码 ZIP。**上传源代码时不要只上传 ZIP 文件本身**，否则 GitHub Actions 找不到 `main.py`、`requirements.txt` 和 `.github/workflows/build-windows.yml`。

## 推荐方式：GitHub Desktop（你的 Windows 电脑不需要安装 Python）

1. 从 ChatGPT 下载 `sql-converter-source.zip` 并解压。
2. 安装或启动 [GitHub Desktop](https://desktop.github.com/)，登录有 `lhuat/sql-converter` 写入权限的账号。
3. `File → Clone repository`，选择 `lhuat/sql-converter`，将其克隆到本地。
4. 将解压后 **sql-converter 目录里的所有文件和文件夹**复制到刚克隆的仓库根目录；特别注意 `.github` 是以点开头的隐藏目录。
5. GitHub Desktop 中填写提交说明 `feat: 初始化 SQL 转换桌面工具`，选择 `Commit to main`，再选择 `Push origin`。
6. 到 GitHub 仓库的 `Actions` 标签页，找到 `Build Windows EXE` 工作流；如果没有自动开始，点击 `Run workflow`。
7. 等待 `test` 和 `build` 两项成功后，进入该运行记录，在 `Artifacts` 下载 `SQLConverter-Windows-x64`。
8. 解压 GitHub 生成的 ZIP，双击 `SQLConverter.exe` 使用。

## 也可以使用 Git 命令（如果已安装 Git）

在终端切到一个空工作目录：

```powershell
git clone https://github.com/lhuat/sql-converter.git
```

打开克隆目录，将下载 ZIP 解压后 `sql-converter` 内全部文件（包括 `.github`）复制到克隆目录，再执行：

```powershell
cd sql-converter
git add .
git commit -m "feat: 初始化 SQL 转换桌面工具"
git push origin main
```

首次 Actions 工作流可能需要在 GitHub UI 中确认启用；如果失败，请打开失败步骤的日志，不要安装 Python 来解决云端构建问题。
