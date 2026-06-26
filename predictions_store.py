#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
预测锁存（append-only）。
- 每场比赛在「开赛前/未完赛」首次被看到时，计算预测并写入 predictions_log.json，永久冻结。
- 已冻结的场次绝不重算/覆盖。
- 赛后只读取冻结的预测，与实际比分对比。

键：home_source_name|away_source_name|date(YYYY-MM-DD)
每条记录：
  {home, away, date, kickoff, frozen_at,
   hxg, axg, pw, pd, pl, top1, top2,
   pred_dir(H/D/A), hcap_side, hcap_line, hcap_name, hcap_label,
   retro(bool 是否为回溯补录)}
"""
import json, os, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "predictions_log.json")


def _key(r):
    return f"{r['home']['source_name']}|{r['away']['source_name']}|{r['kickoff'][:10]}"


def load_log():
    if os.path.exists(LOG):
        try:
            return json.load(open(LOG, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_log(log):
    json.dump(log, open(LOG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def freeze_pending(rows, predict_fn, retro_played=True):
    """
    为尚未冻结的比赛写入预测。
      predict_fn(r) -> dict(hxg,axg,pw,pd,pl,top1,top2,pred_dir,
                            hcap_side,hcap_line,hcap_name,hcap_label,cn_home,cn_away)
    未完赛 → 正常锁存 retro=False。
    已完赛但日志里没有 → 回溯补录 retro=True（仅当 retro_played=True），
        这些不计入「赛前锁存命中率」，只作历史参考。
    返回新增条数。
    """
    log = load_log()
    added = 0
    now = dt.datetime.now().astimezone().isoformat()
    for r in rows:
        k = _key(r)
        if k in log:
            continue  # 已冻结，绝不覆盖
        played = bool(r.get("score") and r["score"].get("ft"))
        if played and not retro_played:
            continue
        p = predict_fn(r)
        log[k] = {
            "home": p["cn_home"], "away": p["cn_away"],
            "date": r["kickoff"][:10], "kickoff": r["kickoff"],
            "frozen_at": now,
            "hxg": round(p["hxg"], 2), "axg": round(p["axg"], 2),
            "pw": p["pw"], "pd": p["pd"], "pl": p["pl"],
            "top1": p["top1"], "top2": p["top2"],
            "pred_dir": p["pred_dir"],
            "dir_label": p["dir_label"],
            "retro": played,  # 已完赛才写=回溯补录
        }
        added += 1
    if added:
        save_log(log)
    return added


def get(r):
    return load_log().get(_key(r))
