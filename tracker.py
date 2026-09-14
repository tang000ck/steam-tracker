"""Steam 饰品价格追踪：读取公开库存 → 查市场价 → 记录历史 → Bark 推送。

本地测试：python tracker.py --no-push
云端运行：由 .github/workflows/update.yml 每小时调用，BARK_KEY 从仓库 Secrets 注入。
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CST = timezone(timedelta(hours=8))
MAX_POINTS = 24 * 180  # 每个序列最多保留约 180 天的小时级数据
NO_PUSH = "--no-push" in sys.argv

session = requests.Session()
session.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def get_json(url, params, tries=4, wait=15):
    """带退避重试的 GET；Steam 限流（429）时指数等待。"""
    for _ in range(tries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 429:
                print(f"  429 限流，等待 {wait}s")
                time.sleep(wait)
                wait *= 2
                continue
            if r.status_code != 200:
                print(f"  HTTP {r.status_code}")
                time.sleep(5)
                continue
            return r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  请求失败: {e}")
            time.sleep(5)
    return None


def fetch_inventory(steamid, appid):
    """返回可上架饰品列表（按 market_hash_name 合并数量）；失败返回 None。"""
    url = f"https://steamcommunity.com/inventory/{steamid}/{appid}/2"
    params = {"l": "schinese", "count": 2000}
    assets, descs = [], {}
    while True:
        d = get_json(url, params, tries=2, wait=10)
        if not d or not d.get("success"):
            return None
        assets += d.get("assets", [])
        for x in d.get("descriptions", []):
            descs[(x["classid"], x["instanceid"])] = x
        if not d.get("more_items"):
            break
        params["start_assetid"] = d["last_assetid"]
        time.sleep(3)

    items = {}
    for a in assets:
        x = descs.get((a["classid"], a["instanceid"]))
        # 交易冷却中的饰品 marketable=0 但有 restriction 天数，之后仍可上架
        if not x or not (x.get("marketable") or x.get("market_marketable_restriction", 0) > 0):
            continue
        key = f"{appid}:{x['market_hash_name']}"
        it = items.setdefault(key, {
            "key": key,
            "app": appid,
            "hash": x["market_hash_name"],
            "name": x.get("market_name") or x.get("name"),
            "type": x.get("type", ""),
            "icon": x.get("icon_url", ""),
            "color": x.get("name_color", ""),
            "count": 0,
            "locked": 0,
        })
        n = int(a.get("amount", 1))
        it["count"] += n
        if not x.get("tradable"):
            it["locked"] += n
    return list(items.values())


def parse_price(s):
    if not s:
        return None
    s = re.sub(r"[^\d.,]", "", s)
    s = s.replace(",", "") if "." in s else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def fetch_price(appid, hash_name, currency):
    d = get_json("https://steamcommunity.com/market/priceoverview/",
                 {"appid": appid, "currency": currency, "market_hash_name": hash_name})
    if not d or not d.get("success"):
        return None
    price = parse_price(d.get("lowest_price")) or parse_price(d.get("median_price"))
    if price is None:
        return None
    volume = int(re.sub(r"\D", "", d.get("volume", "")) or 0)
    return price, volume


def value_ago(series, ts, hours=24):
    """取约 hours 小时前的值；数据不足或断档太久返回 None。"""
    target = ts - hours * 3600
    older = [p for p in series if p[0] <= target + 2 * 3600]
    if not older:
        return None
    t, v = older[-1]
    age = ts - t
    if age < hours * 3600 * 0.8 or age > hours * 3600 * 1.5:
        return None
    return v


def change_pct(new, old):
    if new is None or not old:
        return None
    return round((new - old) / old * 100, 2)


def push(title, body, url=None):
    raw = os.environ.get("BARK_KEY", "").strip()
    if NO_PUSH or not raw:
        print(f"[未推送] {title}\n{body}")
        return
    # 允许直接粘贴 Bark 首页的完整地址
    m = re.search(r"day\.app/([A-Za-z0-9]+)", raw)
    key = m.group(1) if m else raw
    server = os.environ.get("BARK_SERVER", "https://api.day.app").rstrip("/")
    payload = {"device_key": key, "title": title, "body": body, "group": "Steam饰品"}
    if url:
        payload["url"] = url
    try:
        r = session.post(f"{server}/push", json=payload, timeout=20)
        print(f"Bark 推送: HTTP {r.status_code}")
    except requests.RequestException as e:
        print(f"Bark 推送失败: {e}")


def page_url(cfg):
    if cfg.get("page_url"):
        return cfg["page_url"]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


def main():
    cfg = load(ROOT / "config.json", {})
    now = datetime.now(CST)
    ts = int(now.timestamp())
    latest = load(DATA / "latest.json", {"items": []})
    history = load(DATA / "history.json", {"total": [], "items": {}})
    state = load(DATA / "state.json", {"alerts": {}, "daily": ""})
    prev = {it["key"]: it for it in latest.get("items", [])}

    # 库存接口对云服务器 IP 限流很严，且库存很少变化：每隔几小时才刷新一次，失败就沿用上次的清单
    refresh = ts - state.get("inventory_at", 0) >= cfg.get("inventory_hours", 6) * 3600
    items = []
    for app in cfg["apps"]:
        inv = fetch_inventory(cfg["steamid"], app) if refresh else None
        if inv is None:
            inv = [it for it in latest.get("items", []) if it["app"] == app]
            print(f"[{app}] {'库存读取失败，' if refresh else ''}沿用上次清单")
        else:
            state[f"inventory_ok_{app}"] = ts
        print(f"[{app}] {len(inv)} 种饰品")
        items += inv
    if refresh:
        state["inventory_at"] = ts

    fresh = 0
    for i, it in enumerate(items):
        if i:
            time.sleep(cfg.get("request_interval", 4))
        p = fetch_price(it["app"], it["hash"], cfg["currency"])
        old = prev.get(it["key"], {})
        series = history["items"].setdefault(it["key"], [])
        if p:
            fresh += 1
            it["price"], it["volume"], it["stale"] = p[0], p[1], False
            series.append([ts, p[0]])
            del series[:-MAX_POINTS]
        else:
            it["price"], it["volume"], it["stale"] = old.get("price"), old.get("volume"), True
        it["change"] = change_pct(it["price"], value_ago(series, ts))
        print(f"  {it['name']} x{it['count']}: {it['price']} ({it['change']}%){' [旧价]' if it['stale'] else ''}")

    total = round(sum((it["price"] or 0) * it["count"] for it in items), 2)
    if fresh:
        history["total"].append([ts, total])
        del history["total"][:-MAX_POINTS]
    total_change = change_pct(total, value_ago(history["total"], ts))

    latest = {"updated_at": ts, "total": total, "total_change": total_change,
              "fresh": fresh, "items": items}
    link = page_url(cfg)

    # 单件异动提醒（同一饰品冷却期内不重复推送）
    threshold = cfg.get("alert_pct", 10)
    cooldown = cfg.get("alert_cooldown_hours", 12) * 3600
    moves = []
    for it in items:
        c = it["change"]
        if it["stale"] or c is None or abs(c) < threshold:
            continue
        if ts - state["alerts"].get(it["key"], 0) < cooldown:
            continue
        state["alerts"][it["key"]] = ts
        old_price = it["price"] / (1 + c / 100)
        moves.append(f"{'📈' if c > 0 else '📉'} {it['name']}  ¥{old_price:.2f} → ¥{it['price']:.2f}（{c:+.1f}%）")
    if moves:
        push(f"饰品 24h 涨跌超过 {threshold}%", "\n".join(moves), link)

    # 每日总价值日报
    today = now.strftime("%Y-%m-%d")
    if now.hour >= cfg.get("daily_hour", 21) and state.get("daily") != today and fresh:
        state["daily"] = today
        lines = [f"总价值 ¥{total:,.2f}" + (f"（24h {total_change:+.2f}%）" if total_change is not None else "")]
        ranked = sorted((it for it in items if it["change"] is not None), key=lambda x: x["change"])
        if ranked:
            lines.append(f"涨幅最大：{ranked[-1]['name']} {ranked[-1]['change']:+.1f}%")
            lines.append(f"跌幅最大：{ranked[0]['name']} {ranked[0]['change']:+.1f}%")
        push("Steam 库存日报", "\n".join(lines), link)

    save(DATA / "latest.json", latest)
    save(DATA / "history.json", history)
    save(DATA / "state.json", state)
    print(f"完成：总价值 ¥{total}，成功查价 {fresh}/{len(items)}")


if __name__ == "__main__":
    main()
