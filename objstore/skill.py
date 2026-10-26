# skill: objstore
# description: 对象存储（S3 兼容）：上传 / 下载 / 删除 / 列表 / 复制 / 元数据 / bucket / 文件夹 / 预签名
# status: enable
# usage:
#   action（除 list_buckets / bucket 操作外均需 bucket）:
#     upload{bucket,key,local_path} / download{bucket,key,local_path} / delete{bucket,key}
#     delete_batch{bucket,keys} / list{bucket,prefix?,delimiter?,max_keys?,continuation_token?}
#     copy{bucket,key,source_key,source_bucket?} / head{bucket,key}
#     list_buckets{} / create_bucket|delete_bucket{bucket} / create_folder|delete_folder{bucket,folder}
#     presign{bucket,key,method?=GET|PUT,expires?=3600}
#   连接: config.json 或 env 的 S3_ENDPOINT_URL/S3_ACCESS_KEY/S3_SECRET_KEY（可选 S3_REGION/timeout）
#         也可用 arguments 临时覆盖；HTTPS: verify=verify|none / ca_bundle / S3_VERIFY / S3_CA_BUNDLE
#   示例: {"action":"list","bucket":"demo","prefix":"logs/"} / {"action":"presign","bucket":"demo","key":"a.txt"}
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
