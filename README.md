# shell-skills

[shell-tool](https://github.com/abc12524/shell-tool) 及其移动端 [android-agent](https://github.com/abc12524/android-agent) /
[kelivo-agent](https://github.com/abc12524/kelivo-agent) 的**可下载技能仓库**。

每个技能是一个独立文件夹，内含入口脚本与配置模板；根目录 `index.json` 是全部技能的
名称与说明索引。消费方读取 `index.json` 展示可选技能，按需下载对应文件夹到本地
`skill/` / `skills/` 目录，自动注册为可调用技能。

## 目录结构

```
shell-skills/
├── index.json          # 根索引：技能名 + 说明（shell-tool 据此列出可选技能）
├── README.md
├── aria2/              aria2 下载管理（新建 / 列表 / 详情 / 暂停 / 继续 / 删除）
├── at/                 定时任务管理（创建 / 查看 / 删除到点执行一次的命令）
├── baidu_search/       百度搜索 / 百科查询
├── deepseek_search/    DeepSeek 网页搜索 / 抓取（官方自研搜索引擎）
├── doubao_search/      Feedcoop 豆包搜索 API（web_search / global_search）
├── dustls/             磁盘占用分析（dust 纯数据输出，LLM 友好）
├── exif/               图片 EXIF 工具（读取 / 移除 GPS / 移除全部 / 写入 / 备注）
├── ftp/                FTP/FTPS 文件传输（ftplib，兼容 vsftpd：上传 / 下载 / 删除 / 列表）
├── home_assistant/     Home Assistant 智能家居（实体状态 / 服务调用 / 历史日志 / 模板渲染）
├── llm_balance/        大模型平台余额查询（火山引擎 / 智谱 BigModel / DeepSeek）
├── mail/               多邮箱通用工具（列收件箱 / 读邮件 / 发邮件 / 查未读）
├── nga/                NGA 论坛浏览（版块查询 / 帖子 / 回复 / 搜索 / 登录）
├── objstore/           对象存储（S3 兼容）：上传 / 下载 / 删除 / 列表 / 复制 / 预签名
├── ov/                 OpenViking 外置记忆统一入口（搜索 / 写入 / 读取 / 遗忘）
├── qbittorrent/        qBittorrent 下载管理（新建 / 列表 / 详情 / 暂停 / 继续 / 删除）
├── s1/                 Stage1st 论坛浏览（版块 / 帖子 / 回复 / 登录）
├── script/             脚本/代码增删改查 + 结构骨架
├── skill_guide/        如何编写 skill：规范、要求与示例（内置 skill_README.md）
├── smb/                SMB/CIFS 文件传输（smbprotocol：上传 / 下载 / 删除 / 列表）
├── ssh/                SSH 远程执行与 SCP 文件传输（paramiko：exec / 上传 / 下载）
├── storage_audit/      手机存储审计（分区容量 / 大文件 / 目录与类型排行 / 回收站清理）
├── timestamp/          时间戳转换（当前时间 / 时间戳↔日期，支持时区与格式）
├── token_count/        DeepSeek V4 token 计数（直接文本 / 文件路径，纯 Python 零依赖）
├── transmission/       Transmission 下载管理（新建 / 列表 / 详情 / 暂停 / 继续 / 删除）
├── tplink_ptz/         TP-Link 摄像头 PTZ 云台控制
├── wait/               等待指定秒数（最长 600 秒）
└── weather/            wttr.in 天气查询（默认武汉）
```

> 每个技能文件夹内含入口脚本 `skill.py`、可选的配置模板与 `_` 前缀助手模块；
> 各技能参数与依赖以 `index.json` 为准。

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
  （`{"status":"ok","result":{...}}` / `{"status":"error",...}`）。信封的构造见下节
  「信封导入」——需兼容 shell-tool 与移动端两种运行时。
- **操作选择器统一用 `action`**：需要区分多个操作的技能，一律使用
  `arguments.action`（如 `{"action": "list"}`）；不再使用 `cmd` / `mode` / `api`
  等别名。单一功能技能可省略选择器，直接传业务参数。
- **参数命名统一为小写 `snake_case`**；布尔值用 JSON `true` / `false`，
  数组用于多值参数（如 `paths` / `keys` / `args`）。
- **助手模块**以 `_` 开头（如 `_script_impl.py`），不会被注册；入口用相对导入引用：
  `from ._script_impl import ...`。
- **配置与脚本同目录**：技能需要的密钥/参数写在同目录 `config.json`（键为环境变量名）：
  ```json
  { "BAIDU_QIANFAN_KEY": "sk-..." }
  ```
  仓库只提交 `config.example.json`；真实 `config.json` 由安装时生成并写入 `.gitignore`。
  需要结构化/多实例配置的技能（如 `tplink_ptz` 的多摄像头）改用同目录 ini 文件，
  提交 `*.conf.example` 模板、真实配置文件同样写入 `.gitignore`。

## 信封导入（跨运行时兼容）

本仓库技能同时被 shell-tool 与 android-agent / kelivo-agent 消费，两边的信封模块不同：

| 运行时 | 信封模块 | `ok()` 返回 |
|---|---|---|
| shell-tool | `core.tools.envelope` | JSON 字符串 |
| android-agent / kelivo-agent | 技能目录部署的 `_envelope.py` | dict |

技能应按「优先 `_envelope`、回退 `core.tools.envelope`」兼容导入，两个 runner 分别期望
dict / str 信封，导入随环境选择即可正确透传：

```python
try:  # android-agent / kelivo-agent
    from _envelope import ok, error
except ImportError:  # shell-tool
    from core.tools.envelope import ok, error
```

> 只用标准库的技能（如 `at` / `script`）可两处通用；依赖第三方包（如 `requests`）的技能
> 在移动端需先安装对应包。

## index.json 结构

```json
{
  "version": 1,
  "skills": [
    {
      "name": "baidu_search",
      "description": "百度搜索 / 百科查询",
      "platform": ["linux", "windows", "android", "ios"],
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
| `platform` | 适用平台，取值 `linux` / `windows` / `android` / `ios` 的子集；跨平台写全四个 |
| `path` | 技能文件夹相对仓库根的路径 |
| `entry` | 入口脚本文件名（约定 `skill.py`） |
| `version` | 技能版本 |
| `requires` | 额外 Python 依赖（pip 包名） |
| `config` | 需要的配置键（环境变量名）；安装时从 `config.example.json` 生成模板 |

## 平台取值

根索引的 `platforms` 字段声明本仓库支持的平台集合：

| 值 | 含义 |
|---|---|
| `linux` | Linux（含桌面 / 服务器） |
| `windows` | Windows |
| `android` | Android（Termux 等 Python 环境） |
| `ios` | iOS（a-Shell / Pythonista 等） |

每个技能的 `platform` 从上述集合中取子集，表示该技能可运行的平台。


## 消费方如何安装

1. **列远程技能**：拉取本仓库 `index.json`，展示 `name` + `description`。
2. **下载安装**：按 `path` 下载整个文件夹（推荐整仓 tarball 解压子目录，或 GitHub
   contents API），写入本地 `skill/<name>/`（移动端为 `skills/<name>/`）；若该技能有
   `config.example.json` 且本地无 `config.json`，复制一份待填。
3. **注册执行**：扫描到 `<name>/skill.py` 即自动注册（shell-tool 以 `skill.<name>.skill`
   导入；移动端 runner 按文件夹布局导入）；`_` 前缀模块与 `__init__.py` 不注册。
4. **热刷新**：安装后重载注册表即可，无需重启进程（移动端按文件签名自动重扫）。

## 安全

- 下载远程技能 = 在本进程内导入并执行其代码，等于信任本仓库。请仅从可信来源安装。
- 公开仓库**不放任何密钥**；只放 `config.example.json`。
