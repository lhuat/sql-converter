# SQL 转换规则开发说明

## 分层约定

- `scanner.py` 只负责正确拆分 SQL 语句、处理字符串/注释/括号/标识符引号，不能直接删业务 SQL。
- `mappings.py` 维护数据类型模板，例如 `VARCHAR(n)` → `VARCHAR(n)`。用户选择只影响目标类型，不会当作原始 SQL 字符串执行。
- `engine.py` 中的 `Rule` 是语法规则基类。`RULES` 是显式白名单顺序注册表（不能任意从上传的 SQL 文件动态加载 Python 代码）。
- 每个规则都必须调用 `ctx.quotes()` 在合法位置处理标识符，并对方言相关值、复杂表达式进行检查。
- 不支持的情况使用 `raise UnsupportedSQL('唯一规则编码', '中文说明')`。转换器在报告中记为 `BLOCKER`，且不会输出原语句的可执行文本。

## 增加一种转换

1. 从 `*_report.json` 定位 `code`、`line`、`excerpt`，确定问题属于字段类型还是 SQL 语法。
2. 类型差异：在 `mappings.py` 的相应方向里增加带说明的模板，添加目标类型白名单，编写测试。
3. 语法差异：在 `engine.py` 新增 `Rule` 子类，实现 `accepts(sql)` 和 `convert(sql, ctx)`，放入 `RULES` 的正确优先级位置。
4. 为每个新规则添加正向、反向、异常、边界（字符串和注释）四类自动化测试。必须覆盖“不能安全转换”时的阻断行为。
5. 运行 `python -m unittest discover -s tests -v`；通过后提交到 GitHub，触发 Windows 打包。

## 与数据库版本的关系

当前实现的是通用离线转换子集，**不是完整的 openGauss B 模式/Dolphin 方言解析器**。不能根据目标类型名称就断言目标版本支持该 SQL。后续可增加版本适配配置（按版本、兼容模式启用规则）和真实数据库集成测试。切勿把“单元测试通过”当作“Navicat 可直接生产执行”的证明。
