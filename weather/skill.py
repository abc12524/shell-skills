# skill: weather
# description: wttr.in 天气查询（默认武汉）：Markdown 日报 / 一行摘要 / 结构化 JSON，可落盘
# usage:
#   arguments.location = 地点名，如 Wuhan（默认）/ Beijing / "New York"
#   arguments.days     = 0 | 3 ，默认 0
#                        0=当天；3=当天+未来两天；7=当天+未来六天（wttr.in 实际只提供 3 天）
#   arguments.format   = markdown | text | json，默认 markdown
#                        markdown=带表格的中文日报；text=一行摘要；json=结构化数据（不含渲染）
#   arguments.output   = 可选，把 markdown / text 结果写入指定文件，如 /tmp/weather.md
#   返回: result.text 为渲染文本（markdown/text），result.forecast 为逐日结构化数据（json）
#   示例: {"location": "Wuhan"}
#   示例: {"location": "Shanghai", "days": 3}
#   示例: {"location": "Tokyo", "days": 3, "format": "json"}
#   示例: {"location": "Wuhan", "output": "/tmp/weather.md"}
"""wttr.in 天气查询 skill（由原 CLI 脚本 weather.py 按 skill 规范改造）。

改造要点：
  * 文件顶部加 `# skill: weather` + description + usage 头部块，文件名与 skill 名一致；
  * 原 `sys.exit(...)` 改为抛 WeatherError，由 run() 统一转成 error() 信封（带稳定错误码）；
  * 原 `print(...)` 与交互式 input() 去掉，结果统一 `return ok(...)`；
  * import 期零副作用（不发请求、不写文件、不跑 CLI）；
  * 参数统一 `a.get(key, default)`，经校验后才请求，避免 KeyError / 无效取值。

模块底部的 CLI 入口（python skill.py -l Wuhan -d 3）仅供本地调试，skill 执行只调用 run()。
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error

WTTR_URL = "https://wttr.in/{location}?format=j1&lang=zh"
DEFAULT_LOCATION = "Wuhan"
VALID_DAYS = (0, 3, 7)
FORMATS = ("markdown", "text", "json")
TIMEOUT = 15
WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

CODE_CN = {
    "113": "晴", "116": "多云", "119": "阴", "122": "阴",
    "143": "薄雾", "149": "烟霾", "176": "局部小雨", "179": "局部小雪",
    "182": "局部雨夹雪", "185": "局部冻雨",
    "200": "雷阵雨", "227": "风吹雪", "230": "暴风雪",
    "248": "雾", "260": "冻雾",
    "263": "零星小雨", "266": "小雨", "281": "冻毛毛雨",
    "284": "冻雨", "293": "零星小雨", "296": "小雨",
    "299": "间歇中雨", "302": "中雨", "305": "间歇大雨",
    "308": "大雨", "311": "小冻雨", "314": "中到大冻雨",
    "317": "小雨夹雪", "320": "小雪夹雨", "323": "小雪",
    "326": "小雪", "329": "零星小雪", "332": "中雪",
    "335": "零星大雪", "338": "大雪", "350": "冰粒",
    "353": "小阵雨", "356": "中到大阵雨", "359": "暴雨",
    "362": "小阵雪夹雨", "365": "阵雪夹雨",
    "368": "小阵雪", "371": "中到大阵雪",
    "374": "小冰粒", "377": "中到大冰粒",
    "386": "雷阵雨", "389": "强雷阵雨",
    "392": "雷阵雪", "395": "强雷阵雪",
}


class WeatherError(Exception):
    """可控错误：message 面向用户，code 为稳定短标识（http_error / network_error / ...）。"""

    def __init__(self, message, code="internal"):
        super().__init__(message)
        self.code = code


def _first_value(items):
    """wttr.in 字段形如 [{"value": "北京"}]，取出第一个 value。"""
    if isinstance(items, list) and items:
        item = items[0]
        if isinstance(item, dict):
            return item.get("value", "") or ""
    return ""


def fetch_weather(location):
    """从 wttr.in 获取指定地点的 j1 JSON 天气数据；失败抛 WeatherError。"""
    url = WTTR_URL.format(location=urllib.parse.quote(location))
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise WeatherError(f"请求失败: HTTP {e.code} ({location})", code="http_error")
    except urllib.error.URLError as e:
        raise WeatherError(f"网络异常: {e.reason}", code="network_error")
    except TimeoutError:
        raise WeatherError(f"请求超时（>{TIMEOUT}s）: {location}", code="timeout")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise WeatherError("返回数据不是合法 JSON，地点可能不存在。", code="bad_response")

    if not isinstance(data, dict) or not data.get("weather"):
        raise WeatherError(f"未获取到天气数据（地点: {location}）", code="bad_response")
    return data


def place_name(data, location):
    """地点显示名：优先 最近地区 + 国家，取不到则回退用户输入。"""
    area = (data.get("nearest_area") or [{}])[0]
    parts = [_first_value(area.get("areaName")), _first_value(area.get("country"))]
    return ", ".join(p for p in parts if p) or location


def pick_days(data, days):
    """按 days 选择要展示的日期：0=当天；3=当天+未来两天；7=当天+未来六天（受数据长度限制）。"""
    weather_days = data.get("weather") or []
    if not weather_days:
        raise WeatherError("未获取到天气数据。", code="bad_response")
    count = max(1, days + 1) if days >= 0 else 1
    return weather_days[:count]


def describe_code(code):
    """把 wttr.in 的天气代码转成简短中文描述。"""
    return CODE_CN.get(str(code), f"未知({code})")


def fmt_time(time_val):
    """把 "0"/"300" 这类时段转成 "00:00"/"03:00"。"""
    try:
        t = int(time_val)
    except (ValueError, TypeError):
        return str(time_val)
    return f"{t // 100:02d}:{t % 100:02d}"


def _day_desc(day):
    """当日代表性天气：取逐时里出现最多的天气（忽略未知码）。"""
    counts, order = {}, []
    for h in day.get("hourly") or []:
        d = describe_code(h.get("weatherCode", ""))
        if d.startswith("未知"):
            continue
        counts[d] = counts.get(d, 0) + 1
        if d not in order:
            order.append(d)
    return max(order, key=lambda d: counts[d]) if counts else "—"


def _day_rain(day):
    """当日最大降水概率（%）。"""
    vals = [int(h.get("chanceofrain") or 0) for h in (day.get("hourly") or [])]
    return max(vals) if vals else 0


def _day_wind(day):
    """当日最大风速（km/h）。"""
    vals = [int(h.get("windspeedKmph") or 0) for h in (day.get("hourly") or [])]
    return max(vals) if vals else 0


def _date_label(day, idx):
    """日期列：今天 / MM-DD 周X。"""
    if idx == 0:
        return "今天"
    date_str = day.get("date", "")
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{dt.strftime('%m-%d')} {WEEKDAYS_CN[dt.weekday()]}"
    except ValueError:
        return date_str


def fmt_oneline(data, location):
    """生成一行摘要（实况）。"""
    cur = (data.get("current_condition") or [{}])[0]
    return (
        f"{place_name(data, location)}：{describe_code(cur.get('weatherCode', ''))}，"
        f"{cur.get('temp_C', '?')}°C（体感 {cur.get('FeelsLikeC', '?')}°C），"
        f"湿度 {cur.get('humidity', '?')}%，风 {cur.get('windspeedKmph', '?')} km/h"
    )


def day_summary(day, idx):
    """把单日数据整理成结构化 dict（供 format=json）。"""
    date_str = day.get("date", "")
    try:
        weekday = WEEKDAYS_CN[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    except ValueError:
        weekday = ""
    astronomy = (day.get("astronomy") or [{}])[0]
    return {
        "date": date_str,
        "weekday": weekday,
        "mintempC": day.get("mintempC"),
        "maxtempC": day.get("maxtempC"),
        "avgtempC": day.get("avgtempC"),
        "sunrise": astronomy.get("sunrise"),
        "sunset": astronomy.get("sunset"),
        "uvIndex": day.get("uvIndex"),
        "hourly": [
            {
                "time": fmt_time(h.get("time", "")),
                "desc": describe_code(h.get("weatherCode", "")),
                "tempC": h.get("tempC"),
                "feelsLikeC": h.get("FeelsLikeC"),
                "windspeedKmph": h.get("windspeedKmph"),
                "humidity": h.get("humidity"),
                "chanceofrain": h.get("chanceofrain"),
            }
            for h in (day.get("hourly") or [])
        ],
    }


def build_markdown(data, location, days):
    """生成完整 Markdown：一张逐日汇总表 + 今日逐时表（不逐日重复罗列）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    days_list = pick_days(data, days)
    lines = [
        f"# 🌤️ {place_name(data, location)} 天气",
        "",
        f"> 来源 wttr.in ｜ 更新 {now}",
        "",
        "| 日期 | 天气 | 气温 | 降水 | 风 | 日出/日落 | UV |",
        "|------|------|------|------|----|-----------|----|",
    ]
    for i, day in enumerate(days_list):
        astro = (day.get("astronomy") or [{}])[0]
        lines.append(
            f"| {_date_label(day, i)} | {_day_desc(day)} "
            f"| {day.get('mintempC', '?')}～{day.get('maxtempC', '?')}°C "
            f"| {_day_rain(day)}% | {_day_wind(day)} km/h "
            f"| {astro.get('sunrise', '?')} / {astro.get('sunset', '?')} "
            f"| {day.get('uvIndex', '?')} |"
        )

    hourly = days_list[0].get("hourly") or []
    if hourly:
        lines += [
            "",
            "**今日逐时**",
            "",
            "| 时段 | 天气 | 温度 | 体感 | 湿度 | 降水 |",
            "|------|------|------|------|------|------|",
        ]
        for h in hourly:
            lines.append(
                f"| {fmt_time(h.get('time', ''))} | {describe_code(h.get('weatherCode', ''))} "
                f"| {h.get('tempC', '?')}°C | {h.get('FeelsLikeC', '?')}°C "
                f"| {h.get('humidity', '?')}% | {h.get('chanceofrain', '?')}% |"
            )
    lines.append("")
    return "\n".join(lines)


def run(arguments):
    """skill 入口：arguments -> {"status": "ok", "result": {...}} / error 信封。"""
    a = arguments or {}

    location = str(a.get("location") or "").strip() or DEFAULT_LOCATION

    raw_days = a.get("days", 0)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        return error(f"arguments.days 必须是整数（0/3/7），收到 {raw_days!r}", code="bad_request")
    if days not in VALID_DAYS:
        return error(f"arguments.days 只能是 0 / 3 / 7，收到 {days}", code="bad_request")

    fmt = str(a.get("format") or "markdown").strip().lower()
    if fmt not in FORMATS:
        return error(f"arguments.format 只能是 {' / '.join(FORMATS)}，收到 '{fmt}'", code="bad_request")

    output = a.get("output")
    output = str(output).strip() if output else ""

    try:
        data = fetch_weather(location)
        selected = pick_days(data, days)
    except WeatherError as e:
        return error(str(e), code=e.code)

    result = {
        "location": location,
        "place": place_name(data, location),
        "days": days,
        "format": fmt,
    }

    if fmt == "json":
        cur = (data.get("current_condition") or [{}])[0]
        result["current"] = {
            "desc": describe_code(cur.get("weatherCode", "")),
            "tempC": cur.get("temp_C"),
            "feelsLikeC": cur.get("FeelsLikeC"),
            "humidity": cur.get("humidity"),
            "windspeedKmph": cur.get("windspeedKmph"),
            "observationTime": cur.get("localObsDateTime"),
        }
        result["forecast"] = [day_summary(d, i) for i, d in enumerate(selected)]
        return ok(result)

    text = fmt_oneline(data, location) if fmt == "text" else build_markdown(data, location, days)
    result["text"] = text
    result["chars"] = len(text)

    if output:
        try:
            with open(output, "w", encoding="utf-8") as f:
                f.write(text if fmt == "markdown" else text + "\n")
        except OSError as e:
            return error(f"写入 {output} 失败: {e}", code="io_error")
        result["saved_to"] = output

    return ok(result)


def parse_args():
    """本地调试用 CLI（skill 执行不经过这里）。"""
    p = argparse.ArgumentParser(
        description="从 wttr.in 读取天气并以 Markdown 输出",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("-l", "--location", help="地点名称，如 Wuhan（默认）/ Beijing / 'New York'")
    p.add_argument(
        "-d", "--days", type=int, choices=list(VALID_DAYS), default=0,
        help="查询天数: 0=当天, 3=未来三天, 7=未来七天 (默认 0)"
    )
    p.add_argument("-f", "--format", choices=list(FORMATS), default="markdown", help="输出格式")
    p.add_argument("-o", "--output", help="输出到指定文件，省略则打印到终端")
    return p.parse_args()


def main():
    """CLI 主流程：参数 -> run() -> 打印（skill 里不用，仅调试）。"""
    args = parse_args()
    payload = {
        "location": args.location or DEFAULT_LOCATION,
        "days": args.days,
        "format": args.format,
    }
    if args.output:
        payload["output"] = args.output

    env = run(payload)
    if isinstance(env, str):
        env = json.loads(env)
    if env.get("status") != "ok":
        print(f"[错误] {env.get('error')}")
        return 1

    res = env.get("result") or {}
    if args.format == "json":
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(res.get("text", ""))
        if res.get("saved_to"):
            print(f"✅ 已写入 {res['saved_to']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
