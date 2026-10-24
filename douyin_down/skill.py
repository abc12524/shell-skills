# skill: douyin_down
# description: 抖音分享链接解析与下载（视频 / 图集，可选画质）
# status: enable
# usage:
#   arguments.action = download（默认）| parse（元数据）| qualities（列画质）| list（核对本地文件）
#   arguments.share = 分享文案 / v.douyin.com 短链 / 作品 URL（必填）
#   arguments.quality = best（默认，最高画质）| h264 | 2160 | 1440 | 1080 | 720 | 540
#   arguments.media = auto（默认）| video | image | all；out_dir（默认技能目录）/ timeout
#   配置: 同目录 config.json —— DOUYIN_COOKIE（必填，含 UIFID）/ DOUYIN_WEBID / DOUYIN_MS_TOKEN
#   示例: {"share": "https://v.douyin.com/xxxxx/"}
import json
import os

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

from . import _impl


def _load_config():
    """把本技能文件夹内的 config.json 注入环境变量（不覆盖已有 env）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(cfg, dict):
        for key, value in cfg.items():
            if value and not os.environ.get(key):
                os.environ[key] = str(value)


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _default_out_dir():
    configured = (os.getenv("DOUYIN_OUT_DIR") or "").strip()
    if configured:
        return os.path.expanduser(configured)
    return os.path.dirname(os.path.abspath(__file__))


def _variant_label(variant):
    if not variant:
        return None
    label = (f"{variant['short']}p {variant['codec']} "
             f"{round(variant['bitrate'] / 1000)}kbps")
    if variant["size"]:
        label += f" {variant['size'] / 1048576:.1f}MiB"
    return label


def _parse_action(work, detail):
    payload = {
        "work_id": work["work_id"],
        "work_type": work["work_type"],
        "nickname": work["nickname"],
        "images_count": len(work["images"]),
    }
    if work["work_type"] == "视频":
        payload["variants"] = [
            _variant_label(item) for item in _impl.video_variants(detail)
        ]
    return payload


def _qualities_action(work, detail):
    variants = [] if work["work_type"] != "视频" else [
        _variant_label(item) for item in _impl.video_variants(detail)
    ]
    return ok({
        "work_id": work["work_id"],
        "work_type": work["work_type"],
        "variants": variants,
    })


def _expected_files(info):
    if info.get("work_type") == "图集":
        return [f"image_{index}.jpg" for index in range(len(info.get("images") or []))]
    names = []
    if info.get("video_cover"):
        names.append("cover.jpg")
    if info.get("video_addr"):
        names.append("video.mp4")
    return names


def _list_action(out_dir, work_id, limit):
    items = []
    if os.path.isdir(out_dir):
        for root, dirs, files in os.walk(out_dir):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            if "info.json" not in files:
                continue
            dirs[:] = []
            try:
                with open(os.path.join(root, "info.json"), encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError):
                continue
            current = str(info.get("work_id") or "")
            if work_id and current != work_id:
                continue
            media = sorted(name for name in files if name != "info.json")
            expected = _expected_files(info)
            items.append({
                "work_id": current,
                "work_type": info.get("work_type"),
                "files": media,
                "missing": [name for name in expected if name not in media],
                "size_mb": round(sum(os.path.getsize(os.path.join(root, name))
                                     for name in media) / 1048576, 1),
                "saved_dir": root,
            })
    items.sort(key=lambda item: item["work_id"], reverse=True)
    if limit > 0:
        items = items[:limit]
    return ok({"count": len(items), "items": items})


def _download_action(work, detail, quality, media, out_dir, timeout):
    if work["work_type"] == "图集" and media == "video":
        return error("该作品是图集，没有视频可下载；改用 media=image 或 auto",
                     code="invalid_argument")
    if work["work_type"] == "视频" and media == "image":
        return error("该作品是视频，没有图片可下载；改用 media=video 或 auto",
                     code="invalid_argument")

    chosen = None
    if work["work_type"] == "视频":
        try:
            work, chosen = _impl.apply_quality(detail, work, quality)
        except ValueError as exc:
            return error(str(exc), code="invalid_argument")
    if work["work_type"] == "图集" and not work["images"]:
        return error("接口没有返回可下载的图集图片", code="not_found")
    if work["work_type"] == "视频" and not work["video_addr"]:
        return error("接口没有返回可下载的视频地址", code="not_found")

    try:
        os.makedirs(out_dir, exist_ok=True)
        folder, files = _impl.download_work(work, out_dir, media, timeout)
    except Exception as exc:  # noqa: BLE001 - 下载失败原因原样带回
        return error(f"下载失败: {exc}", code="download_error")

    return ok({
        "quality": _variant_label(chosen),
        "saved_dir": folder,
        "files": [os.path.basename(path) for path in files],
    })


def run(arguments):
    _load_config()
    args = arguments or {}
    action = str(args.get("action") or "download").strip().lower()
    if action not in ("download", "parse", "qualities", "list"):
        return error(
            f"未知 action '{action}'，可用: download / parse / qualities / list",
            code="unknown_action",
        )
    out_dir = str(args.get("out_dir") or "").strip() or _default_out_dir()
    out_dir = os.path.abspath(os.path.expanduser(out_dir))
    if action == "list":
        return _list_action(out_dir, str(args.get("work_id") or "").strip(),
                            int(_as_float(args.get("limit"), 50)))
    share = str(args.get("share") or "").strip()
    if not share:
        return error("缺少参数 share（分享文案 / 短链 / 作品 URL）",
                     code="invalid_argument")

    quality = str(args.get("quality") or "best").strip().lower()
    if not (quality in ("best", "max", "h264", "avc") or quality.isdigit()):
        return error("quality 只支持 best / h264 / 2160 / 1440 / 1080 / 720 / 540",
                     code="invalid_argument")
    media = str(args.get("media") or "auto").strip().lower()
    if media not in ("auto", "video", "image", "all"):
        return error(f"未知 media '{media}'，可用: auto / video / image / all",
                     code="invalid_argument")
    timeout = _as_float(args.get("timeout"), 20)

    try:
        share_url = _impl.extract_share_url(share)
    except ValueError as exc:
        return error(str(exc), code="invalid_argument")

    def fetch(url):
        return _impl.http_get(url, headers=_impl.doc_headers(),
                              timeout=timeout, allow_redirects=False)

    try:
        work_url = _impl.resolve_share_url(share_url, fetch)
        client = _impl.DouyinClient(timeout=timeout)
        data = client.get_work_info(work_url)
    except RuntimeError as exc:
        return error(str(exc), code="auth")
    except Exception as exc:  # noqa: BLE001 - 网络 / 风控原因原样带回
        return error(f"解析失败: {exc}", code="resolve_error")

    detail = data.get("aweme_detail")
    if not detail:
        return error("detail 接口没有返回 aweme_detail: " + str(data)[:300],
                     code="not_found")
    work = _impl.handle_work_info(detail)

    if action == "parse":
        return ok(_parse_action(work, detail))
    if action == "qualities":
        return _qualities_action(work, detail)
    return _download_action(work, detail, quality, media, out_dir, timeout)
