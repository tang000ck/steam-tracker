"""Steam 饰品价格追踪：读取公开库存 → 查市场价 → 记录历史 → Bark 推送。

本地测试：python tracker.py --no-push
云端运行：由 .github/workflows/update.yml 每小时调用，BARK_KEY 从仓库 Secrets 注入。
"""
import json
import os
import re
import sys
import time
import urllib.parse
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


def read_overview(d):
    """priceoverview 的返回：最低挂单价 + 24 小时成交量。"""
    if not d or not d.get("success"):
        return None
    price = parse_price(d.get("lowest_price")) or parse_price(d.get("median_price"))
    if price is None:
        return None
    return price, int(re.sub(r"\D", "", d.get("volume", "")) or 0)


def fetch_price(appid, hash_name, currency):
    """直连 Steam，只有家宽 IP 能成功，云端会被 429，所以只试一次。"""
    return read_overview(get_json("https://steamcommunity.com/market/priceoverview/",
                                  {"appid": appid, "currency": currency,
                                   "market_hash_name": hash_name}, tries=1))


def steam_proxy_price(appid, hash_name, currency):
    """Steam 屏蔽机房 IP，借公共网页中转服务转发，拿到的仍是官方价。"""
    target = ("https://steamcommunity.com/market/priceoverview/"
              f"?appid={appid}&currency={currency}"
              f"&market_hash_name={urllib.parse.quote(hash_name)}")
    for _ in range(2):
        try:
            r = session.get(os.environ.get("STEAM_PROXY", "https://r.jina.ai/") + target, timeout=60)
            m = re.search(r'\{\s*"success".*?\}', r.text, re.S)
            if m:
                return read_overview(json.loads(m.group(0)))
            print(f"  中转 HTTP {r.status_code}: {r.text[-120:].strip()}")
        except (requests.RequestException, ValueError) as e:
            print(f"  中转失败: {e}")
        time.sleep(10)
    return None


SKINPORT_CACHE = {}


def skinport_prices(appid, currency):
    """Skinport 一次返回整个游戏的价格表，免 key，机房 IP 可用。"""
    if appid in SKINPORT_CACHE:
        return SKINPORT_CACHE[appid]
    table = {}
    # 先取交易冷却中的挂单（便宜一些），再用可立即交易的挂单覆盖，后者更接近 Steam 市场价
    for params in ({"app_id": appid, "currency": currency, "tradable": 0},
                   {"app_id": appid, "currency": currency}):
        try:
            r = session.get("https://api.skinport.com/v1/items", params=params,
                            headers={"Accept-Encoding": "br"}, timeout=60)
            if r.status_code != 200:
                print(f"  Skinport HTTP {r.status_code}")
                continue
            for x in r.json():
                price = x.get("min_price") or x.get("suggested_price")
                if price:
                    table[x["market_hash_name"]] = (float(price), int(x.get("quantity") or 0))
        except (requests.RequestException, ValueError) as e:
            print(f"  Skinport 失败: {e}")
        time.sleep(2)
    print(f"  Skinport[{appid}] 收录 {len(table)} 种饰品")
    SKINPORT_CACHE[appid] = table
    return table


def skinport_history(appid, hash_name, currency):
    """Skinport 的真实成交记录：近 24 小时 / 7 天 / 30 天 / 90 天的区间与笔数。"""
    try:
        r = session.get("https://api.skinport.com/v1/sales/history",
                        params={"app_id": appid, "currency": currency,
                                "market_hash_name": hash_name},
                        headers={"Accept-Encoding": "br"}, timeout=40)
        if r.status_code != 200:
            print(f"  成交历史 HTTP {r.status_code}")
            return None
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  成交历史失败: {e}")
        return None
    if not rows:
        return None
    row = rows[0]
    out = {}
    for span in ("last_24_hours", "last_7_days", "last_30_days", "last_90_days"):
        d = row.get(span) or {}
        if d.get("volume"):
            out[span] = {k: d.get(k) for k in ("min", "max", "avg", "median", "volume")}
    return out or None


def steamdt_price(hash_name):
    """SteamDT 只有 CS2，但能拿到 Steam 官方市场价。"""
    key = os.environ.get("STEAMDT_KEY", "").strip()
    if not key:
        return None
    try:
        r = session.get("https://open.steamdt.com/open/cs2/v1/price/single",
                        params={"marketHashName": hash_name},
                        headers={"Authorization": f"Bearer {key}"}, timeout=30)
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  SteamDT 失败: {e}")
        return None
    if not d.get("success"):
        print(f"  SteamDT: {d.get('errorMsg') or str(d)[:200]}")
        return None
    rows = [x for x in (d.get("data") or []) if x.get("sellPrice")]
    if not rows:
        return None
    steam = [x for x in rows if "STEAM" in str(x.get("platform", "")).upper()]
    row = (steam or rows)[0]
    return float(row["sellPrice"]), int(row.get("sellCount") or 0)


def get_price(it, cfg):
    """按配置的数据源依次尝试，返回 (价格, 在售数量, 来源)。"""
    for src in cfg.get("sources", {}).get(str(it["app"]), ["skinport"]):
        got = None
        if src == "steamdt":
            got = steamdt_price(it["hash"])
        elif src == "skinport":
            got = skinport_prices(it["app"], cfg.get("currency_code", "CNY")).get(it["hash"])
        elif src == "steam":
            got = fetch_price(it["app"], it["hash"], cfg["currency"])
        elif src == "steam_proxy":
            got = steam_proxy_price(it["app"], it["hash"], cfg["currency"])
        if got:
            return got[0], got[1], src
    return None


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
    if os.environ.get("TEST_PUSH"):
        push("测试推送", "能看到这条说明 Bark 通了 ✅", page_url(cfg))
        return
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
            time.sleep(cfg.get("request_interval", 2))
        p = get_price(it, cfg)
        old = prev.get(it["key"], {})
        series = history["items"].setdefault(it["key"], [])
        if p:
            fresh += 1
            it["price"], it["volume"], it["source"], it["stale"] = p[0], p[1], p[2], False
            series.append([ts, p[0]])
            del series[:-MAX_POINTS]
        else:
            it["price"], it["volume"] = old.get("price"), old.get("volume")
            it["source"], it["stale"] = old.get("source"), True
        # Skinport 成交历史变化慢，隔几小时取一次就够
        it["hist"], it["hist_at"] = old.get("hist"), old.get("hist_at", 0)
        if ts - it["hist_at"] >= cfg.get("history_hours", 6) * 3600:
            got = skinport_history(it["app"], it["hash"], cfg.get("currency_code", "CNY"))
            if got:
                it["hist"], it["hist_at"] = got, ts
        it["change"] = change_pct(it["price"], value_ago(series, ts))
        print(f"  {it['name']} x{it['count']}: {it['price']} ({it['change']}%) "
              f"[{it['source'] or '无数据'}]{' 旧价' if it['stale'] else ''}")

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

    # 定期行情汇总（默认每 6 小时一条）
    every = cfg.get("report_hours", 6)
    if fresh and ts - state.get("report_at", 0) >= every * 3600 - 600:
        state["report_at"] = ts
        span = change_pct(total, value_ago(history["total"], ts, hours=every))
        title = f"库存 ¥{total:,.0f}" + (f"（{every}h {span:+.2f}%）" if span is not None else "")
        lines = []
        for it in sorted(items, key=lambda x: -(x["price"] or 0) * x["count"]):
            c = change_pct(it["price"], value_ago(history["items"].get(it["key"], []), ts, hours=every))
            mark = "" if c is None else f"  {c:+.1f}%"
            lines.append(f"{it['name']} ×{it['count']}  ¥{it['price']:.2f}{mark}" if it["price"] else f"{it['name']} ×{it['count']}  无价格")
        push(title, "\n".join(lines), link)

    save(DATA / "latest.json", latest)
    save(DATA / "history.json", history)
    save(DATA / "state.json", state)
    print(f"完成：总价值 ¥{total}，成功查价 {fresh}/{len(items)}")


if __name__ == "__main__":
    main()
