# skill: ssh
# description: SSH 远程执行与 SCP 文件传输（执行命令 / 上传 / 下载，支持目录）
# status: enable
# usage:
#   arguments.action = exec | upload | download | host_key
#     exec      {command}
#     upload    {local_path, remote_path}
#     download  {remote_path, local_path}
#     host_key  {op: list|remove, host, port}  查看 / 删除已保存的主机公钥
#   连接：host / port / username / password | private_key[_path] / passphrase（加密私钥的密码）
#   主机公钥：host_key_policy = accept_new（默认，首次连接成功后保存到 known_hosts）| strict | off
#   示例: {"action": "exec", "host": "1.2.3.4", "username": "root", "password": "p", "command": "uname -a"}
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
