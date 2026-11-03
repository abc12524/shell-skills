# skill: ssh
# description: SSH 远程执行与 SCP 文件传输（执行命令 / 上传 / 下载，支持目录）
# status: enable
# usage:
#   arguments.action = exec | exec_batch | upload | download | host_key | devices
#     exec        {command}                                 远端执行
#     exec_batch  {command, devices: [别名...]}             多设备并发执行
#     upload      {local_path, remote_path}
#     download    {remote_path, local_path}
#     host_key    {op: list|remove, host, port}             已保存的主机公钥
#     devices     {op: list|show|add|remove, device, host, username, ...}  设备别名管理（写入 config.json 的 HOSTS）
#   连接：device（设备别名）或 host / port / username / password | private_key[_path] / passphrase
#   主机公钥：host_key_policy = accept_new（默认，首次连接成功后保存到 known_hosts）| strict | off
#   示例: {"action": "exec", "device": "nuc5", "command": "uname -a"}
try:  # 作为包导入（shell-tool: skill.ssh.skill / 移动端 runner）
    from . import _ssh_impl as _impl
except ImportError:  # 直接以脚本运行：同目录导入
    import _ssh_impl as _impl


def run(arguments):
    a = arguments or {}
    return _impl._emit(_impl.dispatch(a))


if __name__ == "__main__":  # 独立运行：python skill.py '{"action": "exec", "command": "ls"}'
    import json
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
