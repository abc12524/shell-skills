# android-agent skill 说明文档

本目录（`filesDir/skills/`）存放 **skill（自建 Python 工具）**。每个 `*.py` 脚本即一个
可被 LLM 调用的工具；`skill` 工具会扫描本目录并按脚本头部**自动注册**：
**放入脚本即自动生效，删除脚本即自动注销，无需改核心代码。**

> 以 `_` 开头的文件（如 `_skill_runner.py`、`_envelope.py`）是运行时内部文件，不会被注册。

`skill` 工具只负责**调用**已注册的 skill（以及 `delete` 注销），不再负责创建脚本。
新增 skill 的方式是把脚本文件写进本目录（例如用 `system_tool` 的 `python` 动作写文件），
或用 `skill_hub` 工具从远程仓库下载安装。

## 两种布局（可混用）

- **扁平**：`skills/<名称>.py` —— 单文件即可，最简单。
- **文件夹**：`skills/<名称>/skill.py`（入口）—— 可把助手模块（`_` 开头，入口用相对导入
  `from ._helper import ...` 引用）与配置文件（`config.json`，模板同目录 `config.example.json`）
  放在一起；适合逻辑复杂或需要密钥的技能。

两种布局都按**入口文件**头部自动注册；以 `_` 或 `.` 开头的文件/目录不参与注册。

## 目录与运行环境

- 运行 Python：应用内置的 **Python 3.13**（完整标准库 + pip，清华源）。可用 `system_tool` 的
  `python_pip` 安装第三方包（如 `requests`）。
- 脚本工作目录即本目录，可直接 `import` 同目录下的其他脚本或 `_envelope`。
- 执行方式：`skill` 工具的调用会在独立进程中导入脚本并调用 `run(arguments)`。

## 如何编写一个 skill

### 1. 头部元信息（文件最前面连续的 `#` 注释）

```python
# skill: diskusage            # 名称（唯一，供 LLM 调用；通常与文件名一致）
# description: 查看各分区磁盘占用   # 一句话说明（清单与用法里展示）
# status: enable              # 可选，enable（默认）/ disable；disable 不向 LLM 披露
# usage:
#   arguments.path = 要统计的目录（默认 "/"）
#   示例: {"path": "/data"}
```

### 2. `run(arguments)` 函数

脚本体必须定义 `run(arguments: dict)`，返回：

- **dict**：直接作为结果，runner 包成 `{"status":"ok","result":{...}}`；
- **规范信封 dict/字符串**：形如 `{"status":"ok","result":{...}}` 或 `{"status":"error","error":"..."}`，
  runner 原样透传；
- 其他类型：包成 `{"status":"ok","result":{"output": ...}}`。

推荐 `from _envelope import ok, error` 返回信封。**不要在 `run` 里 `print`**（runner 会捕获以免
污染输出，但更应直接 return）。

```python
from _envelope import ok, error

def run(arguments):
    path = arguments.get("path") or "/"
    if not isinstance(path, str):
        return error("path 必须是字符串", code="invalid_argument")
    # ... 业务逻辑 ...
    return ok({"path": path, "result": "..."})
```

## 如何调用（LLM 用法，两段式逐步披露）

`skill` 工具通过 `action` 分发；子工具名即 **skill 名**：

| 调用 | 说明 |
|------|------|
| `skill`（省略 action） | 返回**已注册 skill 清单**（仅名称 + 简短说明），自动反映新增/删除 |
| `skill` + `action=<skill 名>` | 返回该 skill 的用法（description / usage / path） |
| `skill` + `action=<skill 名>` + `params` | 执行该 skill，`params` 即传给 `run()` 的参数字典 |
| `skill` + `action=delete` + `params.skill` | 删除（注销）某个 skill 脚本 |

### 示例

查看用法：

```json
{"action": "text_stats"}
```

执行（`params` 直接作为 `run(arguments)` 的入参）：

```json
{"action": "text_stats", "params": {"text": "hello world"}}
```

注销：

```json
{"action": "delete", "params": {"skill": "text_stats"}}
```

## 约定与建议

- **返回值即工具结果**：LLM 只看到 `run` 的返回值，尽量返回结构化 dict。
- **错误用信封**：失败返回 `error("原因", code="...")`，便于 LLM 自纠。
- **错误码**：`invalid_argument` / `not_found` / `unknown_skill` / `io_error` / `internal` 等。
- **幂等与安全**：涉及删除/写文件等危险操作时，校验参数并在说明中写明。
- **依赖**：优先标准库；需要第三方库时用 `system_tool` 的 `python_pip` 安装。
- **状态控制**：临时下线某 skill，把头部 `# status:` 改为 `disable`（仍可按名调用，只是不出现在清单里）。
