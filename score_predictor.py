#!/usr/bin/env python3
"""
世界杯比分预测滚动推送脚本

功能：
- 从 fixtures.json 读取赛程，按 kickoff 时间选取最近/下一批 4 场比赛
- 对每场比赛输出最可能的前 2 个比分预测
- 支持单次输出，也支持定时滚动推送
- 支持飞书群机器人 webhook；未配置 webhook 时 dry-run 输出到控制台

用法示例：
  python score_predictor.py --fixtures sample_fixtures.json --once --dry-run
  FEISHU_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/xxx' python score_predictor.py --fixtures fixtures.json --once
  python score_predictor.py --fixtures fixtures.json --watch --interval 300
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclasses.dataclass(frozen=True)
class Team:
    name: str
    rating: float = 1500.0
    attack: float = 1.0
    defense: float = 1.0
    recent_goals_for: float = 1.35
    recent_goals_against: float = 1.10


@dataclasses.dataclass(frozen=True)
class Fixture:
    kickoff: dt.datetime
    home: Team
    away: Team
    competition: str = "世界杯"
    venue: str = ""


def parse_datetime(value: str) -> dt.datetime:
    """Parse ISO-like datetime. Naive times are treated as local time."""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"无法解析 kickoff 时间：{value!r}，请使用 ISO 格式，例如 2026-06-12T21:00:00+08:00") from exc
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def load_fixtures(path: str) -> List[Fixture]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError("fixtures 文件必须是 JSON 数组")

    fixtures: List[Fixture] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i} 条 fixture 不是对象")
        for key in ("kickoff", "home", "away"):
            if key not in item:
                raise ValueError(f"第 {i} 条 fixture 缺少字段：{key}")

        def team_from(prefix: str) -> Team:
            t = item[prefix]
            if isinstance(t, str):
                return Team(name=t)
            if isinstance(t, dict):
                return Team(
                    name=str(t.get("name", "")).strip(),
                    rating=float(t.get("rating", 1500)),
                    attack=float(t.get("attack", 1.0)),
                    defense=float(t.get("defense", 1.0)),
                    recent_goals_for=float(t.get("recent_goals_for", 1.35)),
                    recent_goals_against=float(t.get("recent_goals_against", 1.10)),
                )
            raise ValueError(f"第 {i} 条 fixture 的 {prefix} 必须是字符串或对象")

        home = team_from("home")
        away = team_from("away")
        if not home.name or not away.name:
            raise ValueError(f"第 {i} 条 fixture 的球队名称不能为空")
        fixtures.append(
            Fixture(
                kickoff=parse_datetime(str(item["kickoff"])),
                home=home,
                away=away,
                competition=str(item.get("competition", "世界杯")),
                venue=str(item.get("venue", "")),
            )
        )
    return sorted(fixtures, key=lambda x: x.kickoff)


def poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def negative_binomial(k: int, mean: float, dispersion: float) -> float:
    """Negative-binomial PMF parameterized by mean and dispersion r.

    Variance = mean + mean^2 / r. Larger r approaches Poisson. This captures
    football score over-dispersion: red cards, game-state effects and tactical
    collapses make 3+ goal tails a bit fatter than a pure Poisson model.
    """
    r = max(0.5, dispersion)
    p = r / (r + max(mean, 1e-9))
    log_prob = (
        math.lgamma(k + r)
        - math.lgamma(r)
        - math.lgamma(k + 1)
        + r * math.log(p)
        + k * math.log(1 - p)
    )
    return math.exp(log_prob)


def hybrid_goal_pmf(k: int, mean: float, dispersion: float, nb_weight: float = 0.35) -> float:
    """Blend Poisson with Negative Binomial for scoreline probabilities."""
    w = max(0.0, min(nb_weight, 1.0))
    return (1 - w) * poisson(k, mean) + w * negative_binomial(k, mean, dispersion)


def expected_goals(home: Team, away: Team) -> Tuple[float, float]:
    """
    A transparent lightweight model:
    - base World Cup scoring level around 1.35 goals/team
    - home/nominal-home edge is small because World Cup is mostly neutral
    - Elo-like rating difference affects scoring expectation
    - attack/defense and recent GF/GA adjust final xG

    This is meant as an explainable baseline. For betting-grade forecasts, replace
    team inputs with live market odds, xG, injury and lineup data.
    """
    base = 1.35
    neutral_home_edge = 1.06
    rating_scale = 400.0
    home_strength = 10 ** ((home.rating - away.rating) / rating_scale)
    away_strength = 10 ** ((away.rating - home.rating) / rating_scale)

    home_form = 0.55 * home.recent_goals_for + 0.45 * away.recent_goals_against
    away_form = 0.55 * away.recent_goals_for + 0.45 * home.recent_goals_against

    home_xg = base * neutral_home_edge * home.attack * away.defense * (home_strength ** 0.22) * (home_form / base) ** 0.35
    away_xg = base / neutral_home_edge * away.attack * home.defense * (away_strength ** 0.22) * (away_form / base) ** 0.35

    # Avoid absurd tails if inputs are bad.
    return max(0.15, min(home_xg, 4.5)), max(0.15, min(away_xg, 4.5))


def scoreline_probabilities(
    home: Team,
    away: Team,
    max_goals: int = 7,
    model: str = "hybrid",
    dispersion: float = 7.5,
    nb_weight: float = 0.35,
) -> List[Tuple[int, int, float]]:
    hxg, axg = expected_goals(home, away)
    probs: List[Tuple[int, int, float]] = []
    for hg in range(max_goals + 1):
        if model == "poisson":
            hp = poisson(hg, hxg)
        elif model == "negative-binomial":
            hp = negative_binomial(hg, hxg, dispersion)
        else:
            hp = hybrid_goal_pmf(hg, hxg, dispersion, nb_weight=nb_weight)
        for ag in range(max_goals + 1):
            if model == "poisson":
                ap = poisson(ag, axg)
            elif model == "negative-binomial":
                ap = negative_binomial(ag, axg, dispersion)
            else:
                ap = hybrid_goal_pmf(ag, axg, dispersion, nb_weight=nb_weight)
            probs.append((hg, ag, hp * ap))
    probs.sort(key=lambda x: x[2], reverse=True)
    return probs


def top_scorelines(
    home: Team,
    away: Team,
    top_n: int = 2,
    max_goals: int = 7,
    model: str = "hybrid",
    dispersion: float = 7.5,
    nb_weight: float = 0.35,
) -> List[Tuple[int, int, float]]:
    return scoreline_probabilities(
        home,
        away,
        max_goals=max_goals,
        model=model,
        dispersion=dispersion,
        nb_weight=nb_weight,
    )[:top_n]


def explain_prediction(f: Fixture) -> str:
    hxg, axg = expected_goals(f.home, f.away)
    rating_gap = f.home.rating - f.away.rating
    if abs(rating_gap) < 60:
        strength = "双方评分接近"
    elif rating_gap > 0:
        strength = f"{f.home.name}评分优势约{rating_gap:.0f}分"
    else:
        strength = f"{f.away.name}评分优势约{abs(rating_gap):.0f}分"

    total_xg = hxg + axg
    if total_xg < 2.35:
        tempo = "总进球期望偏低，低比分更集中"
    elif total_xg > 3.05:
        tempo = "总进球期望偏高，2球以上比分更有竞争力"
    else:
        tempo = "总进球期望中等，1-1/1-0/2-1类比分更集中"

    return (
        f"理由：模型xG {f.home.name} {hxg:.2f} - {axg:.2f} {f.away.name}；"
        f"{strength}；{tempo}。"
    )


def select_recent_or_next(fixtures: Sequence[Fixture], count: int, now: Optional[dt.datetime] = None) -> List[Fixture]:
    """Select the next `count` fixtures. If all are in the past, use the latest `count`."""
    now = now or dt.datetime.now().astimezone()
    upcoming = [f for f in fixtures if f.kickoff >= now]
    if upcoming:
        return upcoming[:count]
    return list(fixtures[-count:])


def format_message(fixtures: Sequence[Fixture], generated_at: Optional[dt.datetime] = None) -> str:
    generated_at = generated_at or dt.datetime.now().astimezone()
    lines = [
        "🏆 世界杯最近4场比赛比分预测 Top2",
        f"生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "说明：概率为基于球队 rating/攻防/近期进失球的 Poisson-负二项混合模型估算。",
        "",
    ]
    for idx, f in enumerate(fixtures, start=1):
        preds = top_scorelines(f.home, f.away, top_n=2)
        pred_text = "；".join(f"{f.home.name} {hg}-{ag} {f.away.name}（{prob*100:.1f}%）" for hg, ag, prob in preds)
        venue = f"｜{f.venue}" if f.venue else ""
        lines.extend([
            f"{idx}. {f.kickoff.strftime('%m-%d %H:%M')}｜{f.competition}{venue}",
            f"   {f.home.name} vs {f.away.name}",
            f"   最可能比分：{pred_text}",
            f"   {explain_prediction(f)}",
        ])
    return "\n".join(lines)


def feishu_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_feishu_text(webhook: str, text: str, secret: str = "", timeout: int = 15) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = feishu_sign(secret, ts)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(resp_body)
            except json.JSONDecodeError:
                parsed = {"raw": resp_body}
            parsed["http_status"] = resp.status
            return parsed
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"飞书 webhook HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"飞书 webhook 请求失败：{exc}") from exc


def run_once(args: argparse.Namespace) -> str:
    fixtures = load_fixtures(args.fixtures)
    selected = select_recent_or_next(fixtures, args.count)
    message = format_message(selected)
    print(message)
    webhook = args.webhook or os.getenv("FEISHU_WEBHOOK", "")
    secret = args.secret or os.getenv("FEISHU_SECRET", "")
    if args.dry_run or not webhook:
        print("\n[dry-run] 未发送到飞书：请配置 --webhook 或环境变量 FEISHU_WEBHOOK。", file=sys.stderr)
        return message
    result = send_feishu_text(webhook, message, secret=secret)
    print(f"\n[feishu] send result: {json.dumps(result, ensure_ascii=False)}", file=sys.stderr)
    code = result.get("code", result.get("StatusCode", 0))
    if code not in (0, None):
        raise RuntimeError(f"飞书返回非成功状态：{json.dumps(result, ensure_ascii=False)}")
    return message


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="世界杯比分预测滚动推送到飞书群")
    parser.add_argument("--fixtures", default="fixtures.json", help="赛程 JSON 文件路径")
    parser.add_argument("--count", type=int, default=4, help="每次输出最近/接下来几场比赛，默认 4")
    parser.add_argument("--webhook", default="", help="飞书群机器人 webhook URL，也可用 FEISHU_WEBHOOK")
    parser.add_argument("--secret", default="", help="飞书机器人签名密钥，也可用 FEISHU_SECRET；未启用签名则留空")
    parser.add_argument("--dry-run", action="store_true", help="只输出不发送")
    parser.add_argument("--once", action="store_true", help="只运行一次")
    parser.add_argument("--watch", action="store_true", help="按 interval 秒循环滚动输出/推送")
    parser.add_argument("--interval", type=int, default=300, help="watch 模式推送间隔秒数，默认 300")
    args = parser.parse_args(argv)

    if not args.once and not args.watch:
        args.once = True
    if args.count <= 0:
        parser.error("--count 必须大于 0")
    if args.interval < 30:
        parser.error("--interval 建议不少于 30 秒，避免刷屏")

    if args.watch:
        while True:
            try:
                run_once(args)
            except Exception as exc:  # keep daemon alive, but surface error
                print(f"[error] {exc}", file=sys.stderr)
            time.sleep(args.interval)
    else:
        run_once(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
