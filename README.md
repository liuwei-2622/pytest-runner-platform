# pytest-runner-platform

基于 FastAPI + Jinja2 的本地 pytest 执行平台，用于配置多个 pytest 项目、从网页发起测试运行，并查看测试报告与日志。

## 功能

- 支持配置多个 pytest 项目
- 支持按项目选择测试目标
- 支持每次运行填写环境变量
- 支持 pytest `-k` 关键字表达式
- 支持 pytest `-m` marker 表达式
- 支持最大失败数 `--maxfail`
- 支持 pytest-xdist 并行运行
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

## 运行测试

在首页选择项目后，填写测试目标。

示例：

```text
tests
```

或：

```text
tests/test_demo.py
```

测试目标会按当前项目根目录解析，并且必须位于该项目配置的“允许的测试目录”内。

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

## 报告与日志

每次运行完成后，可以在运行详情页查看：

- pytest HTML 报告
- JUnit XML
- Allure 报告
- Allure Results ZIP
- stdout
- stderr

报告文件默认保存在：

```text
/Users/mac/Documents/pytest/reports/<run_id>/
```

## Allure 支持

平台会自动通过 `allure-pytest` 生成 Allure Results：

```text
reports/<run_id>/allure-results/
```

如果本机安装了 Allure CLI，还会自动生成静态 Allure HTML 报告：

```text
reports/<run_id>/allure-report/
```

如果未安装 Allure CLI，pytest 运行结果不受影响，只是不会生成 Allure HTML 报告。

## 注意事项

- 本平台是本地测试执行平台，当前未包含登录鉴权。
- 项目 Python 环境需要提前安装对应项目的测试依赖。
- 建议每个真实项目配置自己的虚拟环境 Python。
- `允许的测试目录` 建议只配置测试目录，例如 `tests`，不要随意放开到整个项目根目录。
