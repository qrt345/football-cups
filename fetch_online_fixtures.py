#!/usr/bin/env python3
"""
联网抓取世界杯赛程 + World Football Elo 评分，并转换成 score_predictor.py 可用的 fixtures JSON。

数据源：
- 赛程：openfootball/worldcup.json 2026 World Cup JSON
- 评分/近期战绩：World Football Elo Ratings

用法：
  python fetch_online_fixtures.py --output fixtures_online.json
  python score_predictor.py --fixtures fixtures_online.json --once
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

WORLDCUP_URL = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"
ELO_BASE_URL = "https://www.eloratings.net/"

ZH_NAMES = {
    "Algeria": "阿尔及利亚",
    "Argentina": "阿根廷",
    "Australia": "澳大利亚",
    "Austria": "奥地利",
    "Belgium": "比利时",
    "Bosnia & Herzegovina": "波黑",
    "Brazil": "巴西",
    "Canada": "加拿大",
    "Cape Verde": "佛得角",
    "Colombia": "哥伦比亚",
    "Costa Rica": "哥斯达黎加",
    "Croatia": "克罗地亚",
    "Curaçao": "库拉索",
    "Czech Republic": "捷克",
    "DR Congo": "民主刚果",
    "Denmark": "丹麦",
    "Ecuador": "厄瓜多尔",
    "Egypt": "埃及",
    "England": "英格兰",
    "France": "法国",
    "Germany": "德国",
    "Ghana": "加纳",
    "Haiti": "海地",
    "Iran": "伊朗",
    "Iraq": "伊拉克",
    "Ivory Coast": "科特迪瓦",
    "Japan": "日本",
    "Jordan": "约旦",
    "Mexico": "墨西哥",
    "Morocco": "摩洛哥",
    "Netherlands": "荷兰",
    "New Zealand": "新西兰",
    "Nigeria": "尼日利亚",
    "Norway": "挪威",
    "Paraguay": "巴拉圭",
    "Poland": "波兰",
    "Portugal": "葡萄牙",
    "Qatar": "卡塔尔",
    "Saudi Arabia": "沙特阿拉伯",
    "Scotland": "苏格兰",
    "Senegal": "塞内加尔",
    "South Africa": "南非",
    "South Korea": "韩国",
    "Spain": "西班牙",
    "Sweden": "瑞典",
    "Switzerland": "瑞士",
    "Tunisia": "突尼斯",
    "Turkey": "土耳其",
    "Uruguay": "乌拉圭",
    "USA": "美国",
    "United States": "美国",
}

MANUAL_ALIASES = {
    "usa": "US",
    "united states": "US",
    "czech republic": "CZ",
    "czechia": "CZ",
    "bosnia and herzegovina": "BA",
    "bosnia herzegovina": "BA",
    "bosnia & herzegovina": "BA",
    "ivory coast": "CI",
    "cote divoire": "CI",
    "côte divoire": "CI",
    "dr congo": "CD",
    "d r congo": "CD",
    "congo dr": "CD",
    "curacao": "CW",
    "curaçao": "CW",
    "cape verde": "CV",
    "south korea": "KR",
    "south africa": "ZA",
    "new zealand": "NZ",
    "saudi arabia": "SA",
}


def fetch_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 WorldCupPredictor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_json(url: str, timeout: int = 30) -> Any:
    return json.loads(fetch_text(url, timeout=timeout))


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def page_name(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace(" ", "_")
    text = re.sub(r"[^A-Za-z0-9_\-]", "", text)
    return text


def parse_teams_tsv(tsv: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    alias_to_code: Dict[str, str] = {}
    code_to_name: Dict[str, str] = {}
    for line in tsv.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        code = fields[0]
        if code.endswith("_loc") or len(fields) < 2:
            continue
        names = [x for x in fields[1:] if x]
        if not names:
            continue
        code_to_name[code] = names[0]
        for name in names:
            alias_to_code[normalize_name(name)] = code
    alias_to_code.update({normalize_name(k): v for k, v in MANUAL_ALIASES.items()})
    return alias_to_code, code_to_name


def parse_world_ratings(tsv: str) -> Dict[str, Dict[str, float]]:
    ratings: Dict[str, Dict[str, float]] = {}
    for line in tsv.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 31:
            continue
        code = fields[2]
        try:
            total = float(fields[22])
            goals_for = float(fields[29])
            goals_against = float(fields[30])
            rating = float(fields[3])
            rank = float(fields[1])
        except ValueError:
            continue
        ratings[code] = {
            "rating": rating,
            "rank": rank,
            "career_gf": goals_for / total if total else 1.35,
            "career_ga": goals_against / total if total else 1.10,
        }
    return ratings


def parse_kickoff(match: Dict[str, Any]) -> dt.datetime:
    raw_time = str(match["time"])
    m = re.match(r"^(\d{1,2}):(\d{2})\s+UTC([+-]\d{1,2})$", raw_time)
    if not m:
        raise ValueError(f"无法解析赛程时间：{match.get('date')} {raw_time}")
    hour, minute, offset = int(m.group(1)), int(m.group(2)), int(m.group(3))
    tz = dt.timezone(dt.timedelta(hours=offset))
    y, mo, d = [int(x) for x in str(match["date"]).split("-")]
    # 统一转北京时间(UTC+8)，不依赖运行环境时区（云端 runner 是 UTC，否则会显示成 UTC）
    return dt.datetime(y, mo, d, hour, minute, tzinfo=tz).astimezone(dt.timezone(dt.timedelta(hours=8)))


def find_code(name: str, alias_to_code: Dict[str, str]) -> Optional[str]:
    n = normalize_name(name)
    return alias_to_code.get(n)


def clamp(lo: float, value: float, hi: float) -> float:
    return max(lo, min(value, hi))


def recent_team_form(code: str, code_to_name: Dict[str, str], before: dt.datetime, recent_matches: int) -> Tuple[float, float, int]:
    """Return recent goals for/against from Elo team history before kickoff."""
    canonical = code_to_name.get(code)
    if not canonical:
        raise ValueError(f"没有找到球队代码 {code} 对应名称")
    url = urllib.parse.urljoin(ELO_BASE_URL, page_name(canonical) + ".tsv")
    text = fetch_text(url, timeout=30)
    rows: List[Tuple[dt.datetime, float, float]] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        try:
            y, m, d = int(fields[0]), int(fields[1]), int(fields[2])
            if m <= 0 or d <= 0:
                continue
            played_at = dt.datetime(y, m, d, tzinfo=dt.timezone.utc)
            gf1, gf2 = float(fields[5]), float(fields[6])
        except ValueError:
            continue
        if played_at >= before.astimezone(dt.timezone.utc):
            continue
        if fields[3] == code:
            rows.append((played_at, gf1, gf2))
        elif fields[4] == code:
            rows.append((played_at, gf2, gf1))
    rows.sort(key=lambda x: x[0])
    sample = rows[-recent_matches:]
    if not sample:
        raise ValueError(f"没有找到 {canonical} 的近期比赛")
    gf = sum(x[1] for x in sample) / len(sample)
    ga = sum(x[2] for x in sample) / len(sample)
    return gf, ga, len(sample)


def fallback_form(rating_info: Dict[str, float]) -> Tuple[float, float, int]:
    rating = rating_info.get("rating", 1500.0)
    career_gf = rating_info.get("career_gf", 1.35)
    career_ga = rating_info.get("career_ga", 1.10)
    strength = 10 ** ((rating - 1700.0) / 900.0)
    gf = clamp(0.65, 0.65 * career_gf + 0.35 * 1.35 * strength, 2.60)
    ga = clamp(0.50, 0.65 * career_ga + 0.35 * 1.10 / max(strength, 0.25), 2.40)
    return gf, ga, 0


def build_team(name: str, kickoff: dt.datetime, alias_to_code: Dict[str, str], code_to_name: Dict[str, str], ratings: Dict[str, Dict[str, float]], recent_matches: int) -> Dict[str, Any]:
    code = find_code(name, alias_to_code)
    rating_info = ratings.get(code or "", {"rating": 1500.0, "rank": 0.0, "career_gf": 1.35, "career_ga": 1.10})
    try:
        if not code:
            raise ValueError(f"Elo 未匹配到球队：{name}")
        gf, ga, sample_size = recent_team_form(code, code_to_name, kickoff, recent_matches)
        form_note = f"近{sample_size}场"
    except Exception as exc:
        gf, ga, sample_size = fallback_form(rating_info)
        form_note = f"历史均值估计（{exc}）"

    attack = clamp(0.78, (gf / 1.35) ** 0.25, 1.28)
    defense = clamp(0.76, (ga / 1.10) ** 0.25, 1.30)  # lower means stronger defense in score_predictor.py
    display_name = ZH_NAMES.get(name, ZH_NAMES.get(code_to_name.get(code or "", ""), name))
    return {
        "name": display_name,
        "rating": round(float(rating_info.get("rating", 1500.0)), 1),
        "attack": round(attack, 3),
        "defense": round(defense, 3),
        "recent_goals_for": round(gf, 3),
        "recent_goals_against": round(ga, 3),
        "source_name": name,
        "elo_code": code or "",
        "elo_rank": int(rating_info.get("rank", 0) or 0),
        "form_note": form_note,
    }


def build_fixtures(worldcup_url: str, recent_matches: int) -> List[Dict[str, Any]]:
    schedule = fetch_json(worldcup_url)
    matches = schedule.get("matches", []) if isinstance(schedule, dict) else []
    if not matches:
        raise RuntimeError("赛程数据为空或格式不正确")

    teams_tsv = fetch_text(urllib.parse.urljoin(ELO_BASE_URL, "en.teams.tsv"))
    ratings_tsv = fetch_text(urllib.parse.urljoin(ELO_BASE_URL, "World.tsv"))
    alias_to_code, code_to_name = parse_teams_tsv(teams_tsv)
    ratings = parse_world_ratings(ratings_tsv)

    fixtures: List[Dict[str, Any]] = []
    for match in sorted(matches, key=parse_kickoff):
        kickoff = parse_kickoff(match)
        team1 = str(match.get("team1", "")).strip()
        team2 = str(match.get("team2", "")).strip()
        if not team1 or not team2:
            continue
        home = build_team(team1, kickoff, alias_to_code, code_to_name, ratings, recent_matches)
        away = build_team(team2, kickoff, alias_to_code, code_to_name, ratings, recent_matches)
        group = str(match.get("group") or "").strip()
        round_name = str(match.get("round") or "").strip()
        phase = " ".join(x for x in [group, round_name] if x)
        competition = f"世界杯 2026 {phase}".strip()
        fixture: Dict[str, Any] = {
            "kickoff": kickoff.isoformat(),
            "competition": competition,
            "venue": str(match.get("ground") or ""),
            "home": home,
            "away": away,
            "source": "openfootball/worldcup.json + World Football Elo Ratings",
        }
        if "score" in match:
            fixture["score"] = match["score"]
        fixtures.append(fixture)
    return fixtures


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="联网获取世界杯赛程并生成预测输入 JSON")
    parser.add_argument("--output", default="fixtures_online.json", help="输出 JSON 文件")
    parser.add_argument("--worldcup-url", default=WORLDCUP_URL, help="世界杯赛程 JSON URL")
    parser.add_argument("--recent-matches", type=int, default=8, help="计算近期进失球使用的最近比赛场数")
    args = parser.parse_args(argv)

    fixtures = build_fixtures(args.worldcup_url, args.recent_matches)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(fixtures, f, ensure_ascii=False, indent=2)
        f.write("\n")
    upcoming = [f for f in fixtures if "score" not in f]
    print(f"已生成 {args.output}: 总赛程 {len(fixtures)} 场，未完赛/待赛 {len(upcoming)} 场。")
    for f in upcoming[:4]:
        print(f"- {f['kickoff']} {f['home']['name']} vs {f['away']['name']} @ {f.get('venue','')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
