# pytest-runner-platform

基于 FastAPI + Jinja2 的本地 pytest 执行平台，用于配置多个 pytest 项目、从网页发起测试运行，并查看测试报告与日志。

## 功能

- 支持配置多个 pytest 项目
- 支持按项目选择测试路径
- 支持每次运行填写环境变量
- 支持 pytest `-k` 关键字表达式
- 支持 pytest `-m` marker 表达式
- 支持最大失败数 `--maxfail`
- 支持 pytest-xdist 并行运行，可填写 `auto` 或自定义进程数
- 支持 `--lf`、`--ff`、`--tb` 常用 pytest 选项
- 支持保存常用参数组合为模板
- 自动生成 pytest HTML 报告
- 自动生成 JUnit XML 报告
- 自动生成 Allure Results
- 本机安装 Allure CLI 时自动生成 Allure HTML 报告
- 支持查看 stdout / stderr 日志

## 安装依赖

```bash
python3 -m pip install -r requirements.txt
```

## 启动服务

```bash
PYTHONPATH="/Users/mac/Documents/pytest" python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后访问：

```text
http://127.0.0.1:8000
```

## 项目配置

进入页面顶部的“项目配置”，可以新增或编辑 pytest 项目。

主要配置项：

- 项目 ID：项目唯一标识，例如 `demo`
- 项目名称：页面展示名称
- 项目根目录：pytest 项目的根路径
- Python 可执行文件：该项目使用的 Python 解释器
- 工作目录：执行 pytest 时的 cwd
- 允许的测试目录：限制可运行的测试路径，防止执行到非预期目录
- 默认 pytest 参数：每次运行都会附加的 pytest 参数
- 默认环境变量：每次运行都会注入的环境变量

## 推荐使用样例

平台默认内置一个 `demo_project` 示例项目，用来演示推荐配置方式。真实项目不需要放到 `tests_workspace` 下面，只要在“项目配置”里填写实际路径即可。

### 默认 Demo Project

```text
项目 ID: demo
项目名称: Demo Project
项目根目录: /Users/mac/Documents/pytest/demo_project
Python 可执行文件: 当前启动平台使用的 Python
工作目录: /Users/mac/Documents/pytest/demo_project
允许测试目录: /Users/mac/Documents/pytest/demo_project/tests
默认环境变量: DEMO_PROJECT_ENV=ok
```

首页选择 `Demo Project` 后，测试路径填写：

```text
tests
```

或运行单个用例：

```text
tests/test_demo.py::test_demo_passes
```

### 真实项目推荐配置

以真实项目 `ares` 为例，推荐按下面方式配置：

```text
项目 ID: ares
项目名称: Ares API Tests
项目根目录: /Users/mac/Documents/envs/ares
Python 可执行文件: /Users/mac/Documents/envs/ares/.venv/bin/python
工作目录: /Users/mac/Documents/envs/ares
允许测试目录: /Users/mac/Documents/envs/ares/tests
默认 pytest 参数:
  -ra
默认环境变量:
  API_BASE_URL=http://127.0.0.1:8000
```

如果项目的测试目录不叫 `tests`，就填写真实目录，例如：

```text
/Users/mac/Documents/envs/ares/testcases
/Users/mac/Documents/envs/ares/integration_tests
```

推荐原则：

- 项目根目录填真实 pytest 项目根路径。
- 工作目录通常和项目根目录保持一致。
- Python 可执行文件优先填写该项目虚拟环境里的 Python。
- 允许测试目录只放测试目录，不建议直接放开到整个项目根目录。
- 默认环境变量只放团队稳定共用的值；token、账号、临时参数建议每次运行时填写。

## 运行测试

在首页选择项目后，填写测试路径。

示例：

```text
tests
```

或：

```text
tests/test_demo.py
```

测试路径会按当前项目根目录解析，并且必须位于该项目配置的“允许的测试目录”内。

## 环境变量

每次运行可以填写环境变量，每行一个：

```text
API_BASE_URL=http://127.0.0.1:8000
TOKEN=<your-token>
```

运行时环境变量合并顺序：

1. 当前系统环境变量
2. 项目默认环境变量
3. 本次运行填写的环境变量

后面的值会覆盖前面的值。

## 参数模板

首页可以将当前测试路径、过滤条件、并行配置和常用 pytest 选项保存为参数模板。模板按项目区分，应用模板后会回填表单。

模板不会保存环境变量的值，只会保存环境变量名称。应用模板时会生成类似下面的空值占位，实际值需要在运行前重新填写：

```text
API_BASE_URL=
TOKEN=
```

## 常用 pytest 选项

- `workers`：填写 `disabled` 表示不启用 xdist，填写 `auto` 表示让 xdist 自动选择，也可以填写自定义正整数进程数。
- `PYTEST_PLATFORM_MAX_WORKERS`：限制允许填写的最大 xdist 进程数，默认按 CPU 数保守计算，最多 `8`；也可以通过环境变量覆盖。
- `--lf`：只运行上次失败的用例。
- `--ff`：优先运行上次失败的用例。
- `--tb`：可选择 `auto`、`long`、`short`、`line`、`native`、`no`。

## 环境变量校验

运行表单支持每行一个 `KEY=value`，页面会实时展示行级校验和高亮。高亮预览会隐藏环境变量值，只显示变量名和 `******`。

## 报告与日志

每次运行完成后，可以在运行详情页查看：

- 集成测试报告摘要
- pytest HTML 报告
- JUnit XML
- Allure 报告
- Allure Results ZIP
- stdout
- stderr

详情页会解析本次运行生成的 `junit.xml`，直接展示总数、通过、失败/错误、跳过、耗时，以及失败/错误用例明细。原始报告和日志链接仍然保留。

报告文件默认保存在：

```text
/Users/mac/Documents/pytest/reports/<run_id>/
```

## 运行历史与趋势

运行记录页会基于本地 `reports/<run_id>/metadata.json` 和进度信息展示总运行数、通过率、平均耗时、最近运行趋势和最近失败记录。当前实现为本地文件解析，不依赖数据库。

## Allure 支持

平台默认会用目标项目的 Python 环境自动探测报告插件：

- 始终生成 pytest 内置的 JUnit XML，用于平台内置摘要。
- 如果目标项目环境安装了 `pytest-html`，会生成 pytest HTML 报告。
- 如果目标项目环境安装了 `allure-pytest`，会生成 Allure Results：

```text
reports/<run_id>/allure-results/
```

如果本机安装了 Allure CLI，还会自动生成静态 Allure HTML 报告：

```text
reports/<run_id>/allure-report/
```

可通过 `PYTEST_PLATFORM_REPORT_PLUGIN_MODE` 调整报告插件参数策略：

- `auto`：默认值，按目标 Python 环境已安装插件自动添加参数。
- `builtin`：只使用 pytest 内置 JUnit XML，不添加 pytest-html / Allure 参数。
- `strict`：始终添加 pytest-html / Allure 参数，适合要求目标环境必须安装完整报告插件的场景。

如果未安装 Allure CLI，pytest 运行结果不受影响，只是不会生成 Allure HTML 报告。

## 注意事项

- 本平台是本地测试执行平台，当前未包含登录鉴权。
- 项目 Python 环境需要提前安装对应项目的测试依赖。
- 建议每个真实项目配置自己的虚拟环境 Python。
- `允许的测试目录` 建议只配置测试目录，例如 `tests`，不要随意放开到整个项目根目录。
