# witr 使用说明（Why Is This Running）

`witr` 是一个单文件静态二进制，回答一个问题：**这个东西为什么在跑？**
它把进程名、PID、端口、被打开的文件、容器统一归到某个 PID，再把这个 PID 的
「谁拉起了谁」因果链、主管来源（systemd / pm2 / docker / cron / shell）、
工作目录、监听地址、风险警告一次性打印出来。

`ps` / `top` / `lsof` / `ss` / `systemctl` 只给状态和元数据，因果要自己脑补；
`witr` 直接把因果排成一条链。排查「端口被占却不敢 kill」「进程被谁反复拉起」
「这个服务到底归谁管」时先上它。

> 本说明按 **witr v0.3.3**（`witr --version`，2026-06 构建）实测输出核对。

## 1. 常用命令（targets: name / pid / port / file / container）

位置参数（不带 flag）按**进程名或服务名**查，默认子串模糊匹配。

```bash
witr nginx                    # 按名字查（模糊）
witr bun --exact              # 精确匹配，等价 -x
witr nginx node               # 多个名字

witr --pid 1234               # 按 PID，等价 -p
witr --port 8080              # 谁在占这个端口，等价 -o
witr --file /var/lib/dpkg/lock  # 谁打开了这个文件，等价 -f
witr --container redis        # 哪个容器，等价 -c
```

所有目标旗标都可重复、可互相混用，结果按顺序分块展示：

```bash
witr --port 8080 --port 3000
witr --pid 1234 --pid 5678
witr nginx --pid 1234 --port 8080
```

`--container` 覆盖 Docker / Podman / nerdctl / K8s(crictl) / Incus / LXC / LXD /
FreeBSD jail，按容器名、镜像、命令、compose 的 project/service 标签或容器 ID
（完整、短 ID、至少 4 位前缀）匹配。

## 2. 输出模式（short / tree / json / env / warnings / verbose）

默认是完整报告。只要链路或只要某一类信息时切模式：

| 命令 | 输出 |
|---|---|
| `witr nginx --short` / `-s` | 一行祖先链，适合脚本和扫一眼 |
| `witr nginx --tree` / `-t` | 缩进树：祖先在上、目标进程高亮、子进程在下（最多 10 个） |
| `witr nginx --warnings` | 只打警告（Process / Command / Warnings 三段） |
| `witr nginx --env` | 只打环境变量（Process / Command / Environment） |
| `witr nginx --verbose` | 扩展信息：内存、I/O、文件描述符等 |
| `witr nginx --json` | 机器可读 JSON，给脚本和 agent 用 |
| `witr nginx --no-color` | 关闭颜色，便于重定向 / CI |
| `witr` 或 `witr -i` | 交互式 TUI 面板浏览全部（需要 tty） |

模式可叠加，例如 `witr --port 8080 --env --json`。

## 3. 输出字段怎么读（why-it-exists / source）

`witr --port 3000` 的典型输出（真实样例，已裁剪 socket 列表）：

```
Target      : gitea
Process     : gitea (pid 1246) [high-cpu]
User        : git
Service     : gitea.service
Command     : /opt/gitea/gitea web -c /opt/gitea/app.ini
Started     : 17 days ago (Fri 2026-10-23 11:52:39 +08:00)

Why It Exists :
  systemd (pid 1) → gitea (pid 1246)

Source      : gitea.service (systemd)
Description : Gitea (Git with a cup of tea)
Unit File   : /etc/systemd/system/gitea.service

Working Dir : /opt/gitea
Sockets     : 0.0.0.0:3000 (TCP | LISTENING)
              [::]:3000 (TCP6 | LISTENING)

Warnings    :
  • Process is using high CPU (>2h total)
  • Process is listening on a public interface
```

按这个顺序读：

- **Why It Exists**：核心。箭头从左到右 = 谁拉起了谁。
  `systemd → pm2 → node` 说明 node 是 pm2 管的，pm2 又是 systemd 管的；
  此时 `kill node` 会被 pm2 立刻拉回，要动的是 pm2 的配置或 `pm2 delete`。
- **Source**：唯一主来源，决定你该用哪把刀停它 ——
  systemd unit → `systemctl stop <unit>`；docker → `docker stop`；
  pm2 → `pm2 delete`；cron → 改 crontab；shell 会话 → 直接 `kill`。
- **Description / Unit File**：这个服务的身份档案，改配置时直接拿去定位文件。
- **Working Dir / Git Repo / Container**：上下文，判断跑的是哪个副本、哪个分支。
- **Sockets**：监听地址是 `127.0.0.1` 还是 `0.0.0.0` / `[::]`，直接决定暴露面。
- **Warnings**：非阻塞提示。常见的有：以 root 运行、监听公网接口（0.0.0.0/::）、
  重启次数偏多、常驻内存过大（>1GB RSS）、运行超过 90 天、累计 CPU 偏高。

## 4. 退出码（exit-code）

| 码 | 含义 |
|---|---|
| 0 | 干净：找到目标且无警告 |
| 1 | 找到目标，但有一条以上警告 |
| 2 | 未找到匹配的进程 / 服务（root 或 Windows 下端口无人持有也算 2） |
| 3 | 权限不足，用 `sudo` 重试 |
| 4 | 参数错误或匹配歧义（多候选让你挑） |
| 5 | 内部错误 |
| 6 | 找到了进程，但追溯不出是谁启动的（警告里会说明原因） |

多目标时取最严重的结果：2–5 的失败优先于 6，6 优先于 1。

脚本里可直接分流：

```bash
witr nginx --short
case $? in
  0) echo "正常" ;;
  1) echo "有警告" ;;
  2) echo "没在跑" ;;
  3) echo "需要 sudo" ;;
  4) echo "参数或歧义" ;;
  *) echo "其它异常" ;;
esac
```

## 5. 典型排查场景（recipes）

**端口被占，先确认能不能杀**

```bash
witr --port 8080
```

先看 `Source`：是 systemd unit 就 `systemctl stop`，是容器就 `docker stop`，
是 pm2 就 `pm2 delete`。直接 `kill` 只是把上层 supervisor 的重启逻辑触发一次。

**进程反复被拉起**

```bash
witr <名字> --short     # 一行看清父链
witr <名字> --tree      # 连子进程一起看
```

**文件被谁占着 / 删不掉**

```bash
witr --file /var/log/app.log
```

**只看风险，不看详情**

```bash
witr <名字> --warnings
```

适合巡检：公网监听、root 运行、超长运行时间一眼看完。

**给脚本或上层 agent 用**

```bash
witr --port 8080 --json
```

JSON 顶层结构（v0.3.3 实测）：`Target{Type,Value}`、`ResolvedTarget`、
`Process{PID,PPID,Command,Cmdline,Exe,StartedAt,User,CPUPercent,MemoryRSS,
MemoryPercent,WorkingDir,GitRepo,GitBranch,Container,Service,Sockets[]}`，
`Sockets` 每项为 `{Inode,Port,Address,State,Protocol}`。

**远程机器上查**

```bash
ssh root@host 'witr --port 8080 --no-color'
```

非 tty 环境必须带目标，否则会走 TUI 并直接退出 4。

## 6. 坑与限制（limits）

- **名字是子串模糊匹配**，`sshd` 会同时命中 `sshd-session`，直接退出 4 并列出候选。
  候选里挑一个用 `--pid` 重跑，或加 `--exact` 精确匹配。
- **非 root 看不全**：别的用户的进程、部分 socket 信息会缺失或报 3，排查系统服务请 `sudo`。
- **TUI 要终端**：脚本、管道、CI 里别不带目标跑 `witr`。
- **`--tree` 的子进程最多显示 10 个**，更多的折叠成 `... and N more`。
- **端口查不到进程时退出 2**：可能是端口没在监听，也可能是权限不足导致看不到持有者。
- 容器类目标依赖本机有对应 runtime/CLI 可读；纯 cgroup 环境信息可能不全。

## 7. 与常见工具的分工（compare）

| 想知道 | 平时用 | witr |
|---|---|---|
| 有哪些进程、占多少 CPU/内存 | `ps` / `top` | 附带，不是重点 |
| 端口被谁监听 | `ss -lntp` / `lsof -i` | `witr --port 8080`，连带给出启动链和归属 |
| 谁打开了某个文件 | `lsof <file>` | `witr --file <file>` |
| 服务归谁管、配置文件在哪 | `systemctl status` | `witr <svc>`，Source + Unit File 一处给全 |
| 为什么杀了又活 | 手工翻 ps 父链 | `witr <name> --short` / `--tree` |

结论：**排查现状仍用 ps/ss/systemctl，问「为什么」用 witr。**
