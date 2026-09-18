"""珠海校区天气提醒：查 Open-Meteo（免key）→ 判断带伞/加衣 → Bark 推送。

云端运行：由 .github/workflows/weather.yml 在 7:30 和 13:45（北京时间）各跑一次，
BARK_KEY 复用 steam-tracker 已有的仓库 Secret。
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

LAT, LON = 22.41, 113.57  # 中大珠海校区
CST = timezone(timedelta(hours=8))
NO_PUSH = "--no-push" in sys.argv

WCODE = {
    0: "晴", 1: "大致晴", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "阵雪粒",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨",
    95: "雷雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹",
}


def fetch():
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": LAT,
            "longitude": LON,
            "hourly": "temperature_2m,precipitation_probability,weathercode",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "Asia/Shanghai",
            "forecast_days": 1,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def push(title, body):
    raw = os.environ.get("BARK_KEY", "").strip()
    if NO_PUSH or not raw:
        print(f"[未推送] {title}\n{body}")
        return
    m = re.search(r"day\.app/([A-Za-z0-9]+)", raw)
    key = m.group(1) if m else raw
    server = os.environ.get("BARK_SERVER", "https://api.day.app").rstrip("/")
    payload = {"device_key": key, "title": title, "body": body, "group": "天气"}
    resp = requests.post(f"{server}/push", json=payload, timeout=20)
    print(f"Bark 推送: HTTP {resp.status_code}")


def main():
    d = fetch()
    hourly = d["hourly"]
    times = [datetime.fromisoformat(t) for t in hourly["time"]]
    now = datetime.now(CST).replace(tzinfo=None)
    idx = min(range(len(times)), key=lambda i: abs((times[i] - now).total_seconds()))

    temp_now = hourly["temperature_2m"][idx]
    code_now = hourly["weathercode"][idx]
    desc_now = WCODE.get(code_now, "未知")

    hour = now.hour
    if hour < 11:
        # 早上：看白天全天（到20点）的最大降水概率，决定带不带伞；报当天高低温
        end = next((i for i in range(idx, len(times)) if times[i].hour == 20), len(times) - 1)
        window = hourly["precipitation_probability"][idx:end + 1]
        rain_p = max(window) if window else 0
        tmax = d["daily"]["temperature_2m_max"][0]
        tmin = d["daily"]["temperature_2m_min"][0]
        title = f"早安，{desc_now} {temp_now:.0f}°C"
        lines = [f"今天 {tmin:.0f}~{tmax:.0f}°C，白天最高降水概率 {rain_p:.0f}%"]
        if rain_p >= 70:
            lines.append("☔ 大概率下雨，带伞")
        elif rain_p >= 40:
            lines.append("🌂 可能下雨，建议带伞")
        else:
            lines.append("今天不太会下雨")
        if tmax - tmin >= 8:
            lines.append("早晚温差大，外套带上")
    else:
        # 下午：看接下来6小时的最大降水概率
        end = min(idx + 6, len(times) - 1)
        window = hourly["precipitation_probability"][idx:end + 1]
        rain_p = max(window) if window else 0
        title = f"午后提醒，{desc_now} {temp_now:.0f}°C"
        lines = [f"接下来几小时最高降水概率 {rain_p:.0f}%"]
        if rain_p >= 70:
            lines.append("☔ 大概率下雨，记得带伞回去")
        elif rain_p >= 40:
            lines.append("🌂 可能下雨，带把伞保险")
        else:
            lines.append("暂时不用担心下雨")

    push(title, "\n".join(lines))


if __name__ == "__main__":
    main()
