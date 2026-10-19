# skill: objstore
# description: 对象存储（S3 兼容）：上传 / 下载 / 删除 / 列表 / 复制 / 元数据 / bucket / 文件夹 / 预签名
# status: enable
# usage:
#   arguments.action 指定操作：
#     upload         上传本地文件         {bucket, key, local_path}
#     download       下载对象到本地       {bucket, key, local_path}
#     delete         删除对象             {bucket, key}
#     delete_batch   批量删除对象         {bucket, keys: ["k1","k2", ...]}
#     list           列举对象             {bucket, prefix?, delimiter?, max_keys?(默认100), continuation_token?}
#     copy           复制对象             {bucket, key, source_key, source_bucket?}
#     head           读取对象元数据       {bucket, key}
#     list_buckets   列出所有 bucket      {}
#     create_bucket  创建 bucket          {bucket}
#     delete_bucket  删除 bucket          {bucket}
#     presign        生成预签名 URL       {bucket, key, method?: GET|PUT, expires?: 3600}
#     create_folder  创建文件夹           {bucket, folder}
#     delete_folder  删除文件夹及其对象   {bucket, folder}
#   连接参数从同目录 config.json（模板 config.example.json）或环境变量读取，无需传入：
#         S3_ENDPOINT_URL / S3_ACCESS_KEY / S3_SECRET_KEY（可选 S3_REGION）
#   也可用 arguments.endpoint_url / access_key / secret_key / region / timeout 临时覆盖。
#   支持任意 S3 兼容服务：AWS S3、MinIO、Ceph、火山 TOS、阿里 OSS（S3 兼容）等。
#   示例: {"action": "list", "bucket": "demo", "prefix": "logs/"}
#         {"action": "upload", "bucket": "demo", "key": "a.txt", "local_path": "./a.txt"}
#         {"action": "download", "bucket": "demo", "key": "a.txt", "local_path": "./a.txt"}
#         {"action": "presign", "bucket": "demo", "key": "a.txt", "expires": 600}
try:  # 作为包导入（shell-tool: skill.objstore.skill / 移动端 runner）
    from . import _objstore_impl as _impl
except ImportError:  # 直接以脚本运行（脱离 shell-tool）：同目录导入
    import _objstore_impl as _impl


def run(arguments):
    a = arguments or {}
    return _impl._emit(_impl.dispatch(a))


if __name__ == "__main__":  # 独立运行：python skill.py '{"action": "list_buckets"}'
    import json
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
