# skill: tts
# description: Edge TTS 语音合成（文字转 MP3，可调音色 / 语速 / 音量 / 音调）
# status: enable
# usage:
#   arguments.text    = 要合成的文本（必填）
#   arguments.voice   = 音色，默认 zh-CN-XiaoxiaoNeural
#   arguments.rate    = 语速，如 +10%，默认 +0%
#   arguments.volume  = 音量，如 -20%，默认 +0%
#   arguments.pitch   = 音调，如 +5Hz，默认 +0Hz
#   arguments.output  = 输出 MP3 路径，默认 tts/audio/tts_<hash>.mp3
#   正文显示音频：回答里用英文方括号包住「audio:路径」；marker=true 时结果直接附带
#   示例: {"text": "你好，这是一次测试语音。"}
import asyncio
import hashlib
import re
from pathlib import Path

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

_SKILL_DIR = Path(__file__).resolve().parent
_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
_VOICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
_PITCH_RE = re.compile(r"^[+-]\d{1,3}Hz$")


def _modifier(a, key, default, pattern, example):
    """校验 rate/volume/pitch 这类 Edge 修饰参数，返回 (值, 错误信封)。"""
    raw = a.get(key)
    if raw is None or str(raw).strip() == "":
        return default, None
    value = str(raw).strip()
    if not pattern.match(value):
        return None, error(
            f"{key} 格式不合法（示例：{example}）", code="bad_request"
        )
    return value, None


def _output_path(a, text, voice, rate, volume, pitch):
    """确定输出文件路径并创建父目录，返回 (path, 错误信封)。"""
    raw = a.get("output")
    if raw is not None and not isinstance(raw, str):
        return None, error("output 必须是字符串", code="bad_request")

    if raw and raw.strip():
        try:
            path = Path(raw.strip()).expanduser()
            if path.name in ("", ".", "..") or path.is_dir():
                return None, error("output 需要是文件路径，不能是目录", code="bad_request")
            if not path.is_absolute():
                path = _SKILL_DIR / path
            if path.suffix.lower() != ".mp3":
                path = path.with_suffix(".mp3")
            path = path.resolve()
        except (OSError, ValueError) as exc:
            return None, error(f"output 路径不合法: {exc}", code="bad_request")
    else:
        digest = hashlib.sha1(
            "\0".join([text, voice, rate, volume, pitch]).encode("utf-8")
        ).hexdigest()[:12]
        path = _SKILL_DIR / "audio" / f"tts_{digest}.mp3"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, error(f"创建输出目录失败: {exc}", code="io_error")
    return path, None


def _synthesize(edge_tts, text, voice, rate, volume, pitch, path):
    async def _main():
        communicate = edge_tts.Communicate(
            text, voice, rate=rate, volume=volume, pitch=pitch
        )
        await communicate.save(str(path))

    asyncio.run(_main())


def run(arguments):
    a = arguments or {}
    if not isinstance(a, dict):
        return error("arguments 必须是对象", code="bad_request")

    marker = a.get("marker", False)
    if not isinstance(marker, bool):
        return error("marker 必须是布尔值", code="bad_request")

    text = a.get("text")
    if not isinstance(text, str) or not text.strip():
        return error("缺少 text（要合成的文本）", code="missing_argument")

    voice = str(a.get("voice") or _DEFAULT_VOICE).strip()
    if not _VOICE_RE.match(voice):
        return error(
            "voice 格式不合法（示例：zh-CN-XiaoxiaoNeural）", code="bad_request"
        )

    rate, err = _modifier(a, "rate", "+0%", _RATE_RE, "+10%")
    if err:
        return err
    volume, err = _modifier(a, "volume", "+0%", _RATE_RE, "-20%")
    if err:
        return err
    pitch, err = _modifier(a, "pitch", "+0Hz", _PITCH_RE, "+5Hz")
    if err:
        return err

    path, err = _output_path(a, text, voice, rate, volume, pitch)
    if err:
        return err

    try:  # 延迟导入：未安装时仍可注册技能，并由调用方看到明确错误
        import edge_tts
    except ImportError:
        return error(
            "未安装 edge-tts，请先执行 pip install edge-tts",
            code="missing_dependency",
        )

    try:
        _synthesize(edge_tts, text, voice, rate, volume, pitch, path)
    except Exception as exc:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        return error(
            f"语音合成失败（{type(exc).__name__}）: {exc}", code="tts_error"
        )

    try:
        size = path.stat().st_size
    except OSError as exc:
        return error(f"读取音频文件失败: {exc}", code="io_error")
    if size <= 0:
        return error("语音合成未产生有效音频文件", code="tts_error")

    payload = {
        # 用正斜杠：Windows 路径在 JSON 信封里不会产生反斜杠转义
        "path": path.as_posix(),
        "voice": voice,
        "chars": len(text),
        "size_bytes": size,
    }
    if marker:
        payload["audio"] = f"[audio:{path.as_posix()}]"
    return ok(payload)
