# shell-skills

[shell-tool](https://github.com/abc12524/shell-tool) 的**可下载技能仓库**。

每个技能是一个独立文件夹，内含入口脚本与配置模板；根目录 `index.json` 是全部技能的
名称与说明索引。shell-tool 读取 `index.json` 展示可选技能，按需下载对应文件夹到本地
`skill/` 目录，重启（或热刷新）后自动注册为可调用技能。

## 目录结构

```
shell-skills/
├── index.json                 # 根索引：技能名 + 说明（shell-tool 据此列出可选技能）
├── README.md
├── at/
│   └── skill.py               # 入口脚本（头部含 # skill: at）
├── baidu_search/
│   ├── skill.py               # 入口脚本
│   └── config.example.json    # 配置模板（真实 config.json 本地生成、不入库）
├── doubao_search/
│   ├── skill.py
│   └── config.example.json
└── script/
    ├── skill.py
    └── _script_impl.py        # 助手模块（`_` 开头，不注册为技能）
```

## 技能文件夹约定

- **一个文件夹 = 一个技能**，文件夹名即技能名。
- **入口脚本**固定为 `skill.py`；其文件头部按 shell-tool 规范声明：
  ```python
  # skill: <名称，不含空格>
  # description: 一句话说明
  # status: enable            # 可选：enable（默认）/ disable
  # usage:
  #   arguments.foo = ...
  #   示例: {"foo": "bar"}
  ```
- 脚本定义模块级 `def run(arguments: dict) -> str`，返回统一信封
  （`{"status":"ok","result":{...}}` / `{"status":"error",...}`，用 `core.tools.envelope` 的 `ok()/error()`）。
- **助手模块**以 `_` 开头（如 `_script_impl.py`），不会被注册；入口用相对导入引用：
  `from ._script_impl import ...`。
- **配置与脚本同目录**：技能需要的密钥/参数写在同目录 `config.json`（键为环境变量名）：
  ```json
  { "BAIDU_QIANFAN_KEY": "sk-..." }
  ```
  仓库只提交 `config.example.json`；真实 `config.json` 由安装时生成并写入 `.gitignore`。

## index.json 结构

```json
{
  "version": 1,
  "skills": [
    {
      "name": "baidu_search",
      "description": "百度搜索 / 百科查询",
      "path": "baidu_search",
      "entry": "skill.py",
      "version": "1.0.0",
      "requires": ["requests"],
      "config": ["BAIDU_QIANFAN_KEY"]
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `name` | 技能名（唯一，等于文件夹名） |
| `description` | 一句话说明，用于列表展示 |
| `path` | 技能文件夹相对仓库根的路径 |
| `entry` | 入口脚本文件名（约定 `skill.py`） |
| `version` | 技能版本 |
| `requires` | 额外 Python 依赖（pip 包名） |
| `config` | 需要的配置键（环境变量名）；安装时从 `config.example.json` 生成模板 |

## shell-tool 如何消费

1. **列远程技能**：拉取本仓库 `index.json`，展示 `name` + `description`。
2. **下载安装**：按 `path` 下载整个文件夹（推荐整仓 tarball 解压子目录，或 GitHub
   contents API），写入本地 `skill/<name>/`；若该技能有 `config.example.json` 且本地
   无 `config.json`，复制一份待填。
3. **注册执行**：本地扫描到 `skill/<name>/skill.py`，以 `skill.<name>.skill` 导入并调用
   `run(arguments)`；文件夹内的 `_` 前缀模块与 `__init__.py` 不注册。
4. **热刷新**：安装后重载注册表即可，无需重启进程。

## 安全

- 下载远程技能 = 在本进程内导入并执行其代码，等于信任本仓库。请仅从可信来源安装。
- 公开仓库**不放任何密钥**；只放 `config.example.json`。
