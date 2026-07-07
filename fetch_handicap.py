#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓 the-odds-api 的世界杯让球盘（spreads），存成快照 handicap_snapshot.json。
只取让球线(point)，不要赔率。键 = 归一化的 "home_source|away_source"，也存队名兜底匹配。

用法: python fetch_handicap.py
免费层每月 500 次；本脚本一次调用抓全部当前赛事的让球盘。
"""
import urllib.request, json, ssl, os, datetime as dt, re

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
BASE = os.path.dirname(os.path.abspath(__file__))
# key 优先从环境变量读（云端用 Actions Secret 注入），本地回退到 .env.oddsapi
def _load_key():
    k = os.environ.get("ODDS_API_KEY")
    if k:
        return k
    p = os.path.join(BASE, ".env.oddsapi")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "=" in line:
                kk, vv = line.strip().split("=", 1)
                if kk.strip() == "ODDS_API_KEY":
                    return vv.strip()
    return ""
API_KEY = _load_key()
SPORT = "soccer_fifa_world_cup"
OUT = os.path.join(BASE, "handicap_snapshot.json")


def _norm(s):
    """归一化队名用于匹配：小写、去非字母。"""
    return re.sub(r'[^a-z]', '', str(s).lower())


def fetch_spreads():
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds/"
           f"?apiKey={API_KEY}&regions=eu,uk,us&markets=spreads&oddsFormat=decimal")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
        remaining = r.headers.get("x-requests-remaining")
        data = json.loads(r.read().decode())
    return data, remaining


def pick_line(event):
    """从多家 bookmaker 里取一个让球线：优先 Pinnacle，否则第一家。
    返回 {home_team, away_team, home_point, away_point, book}。让球以 home 视角。"""
    home, away = event["home_team"], event["away_team"]
    books = event.get("bookmakers", [])
    # 优先级排序
    pref = {"Pinnacle": 0, "Betfair": 1}
    books.sort(key=lambda b: pref.get(b.get("title", ""), 9))
    for bk in books:
        for mk in bk.get("markets", []):
            if mk["key"] != "spreads":
                continue
            hp = ap = None
            for o in mk["outcomes"]:
                if _norm(o["name"]) == _norm(home):
                    hp = o.get("point")
                elif _norm(o["name"]) == _norm(away):
                    ap = o.get("point")
            if hp is not None:
                return {"home_team": home, "away_team": away,
                        "home_point": hp, "away_point": ap, "book": bk.get("title", "")}
    return None


def main():
    data, remaining = fetch_spreads()
    odds = {}
    for ev in data:
        line = pick_line(ev)
        if not line:
            continue
        key = f"{_norm(line['home_team'])}|{_norm(line['away_team'])}"
        odds[key] = line
    snapshot = {
        "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M 北京时间"),
        "source": "the-odds-api spreads",
        "count": len(odds),
        "handicap": odds,
    }
    json.dump(snapshot, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"已抓取让球盘 {len(odds)} 场，API 剩余额度 {remaining}。写入 {OUT}")
    for k, v in list(odds.items())[:8]:
        print(f"  {v['home_team']} {v['home_point']:+g} / {v['away_team']} {v['away_point']:+g}  [{v['book']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
