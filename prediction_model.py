#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型预测层（底层）。纯算法：不碰HTML、不发网络请求。

职责：
  - xG计算与强弱修正（校准参数集中在这里）
  - 比分概率grid（泊松-负二项混合，阶段感知）
  - 胜平负/让球/大小球等衍生概率
  - 单场预测记录生成（build_prediction，供冻结锁存）
  - 赛后结算（让球/方向）与回测（读冻结日志，不重算）

看板层（generate_dashboard.py）只消费本模块输出。改模型参数只改这里。

校准记录见 预测方法文档.md（2026-07-07校准，2026-07-08同步进代码）。
"""
from __future__ import annotations
import json, os, re

BASE = os.path.dirname(os.path.abspath(__file__))

from score_predictor import expected_goals, hybrid_goal_pmf, Team
import predictions_store as pstore

NAME = {'Uzbekistan': '乌兹别克斯坦', 'Panama': '巴拿马', 'DR Congo': '民主刚果',
        'Ghana': '加纳', 'Croatia': '克罗地亚', 'Portugal': '葡萄牙',
        'England': '英格兰', 'Colombia': '哥伦比亚'}
def cn(x): return NAME.get(x, x)


# ---------- 核心模型 ----------
def mk(td): return Team(name=td['name'], rating=td['rating'], attack=td['attack'], defense=td['defense'],
                        recent_goals_for=td['recent_goals_for'], recent_goals_against=td['recent_goals_against'])


def adjxg(h, a, hx, ax):
    """强弱修正五档（2026-07-07校准）：顶级强队敢打穿，中等差距重点防平。"""
    gap = abs(h.rating - a.rating); ratio = max(hx, ax) / max(0.01, min(hx, ax)); sh = hx >= ax
    top_elo = max(h.rating, a.rating)
    if top_elo >= 2050 and gap >= 180: sf, wf = 0.98, 1.03
    elif gap >= 400 or ratio >= 3.5:   sf, wf = 0.99, 1.03
    elif gap >= 220 or ratio >= 2.3:   sf, wf = 0.93, 1.06
    elif gap >= 120 or ratio >= 1.6:   sf, wf = 0.92, 1.06
    else:                              sf, wf = 0.96, 1.04
    return (hx*sf, ax*wf) if sh else (hx*wf, ax*sf)


# 平局修正从16强开始：本届32强轮90分钟平局率与小组赛接近，16强起球队才真拼胜负
KNOCKOUT_KEYS = ('Round of 16', 'Quarter-final', 'Semi-final', 'third place', 'Final')

def is_knockout(r):
    return any(k in r.get('competition', '') for k in KNOCKOUT_KEYS)


def grid(home, away, knockout=False):
    """比分概率grid。淘汰赛（16强起）：dispersion 8.5尾部更肥 + 平局×0.8再归一化。"""
    h, a = mk(home), mk(away); hx, ax = expected_goals(h, a); hx, ax = adjxg(h, a, hx, ax)
    disp = 8.5 if knockout else 7.5
    g = {}
    for hg in range(9):
        for ag in range(9):
            g[(hg, ag)] = hybrid_goal_pmf(hg, hx, disp, nb_weight=0.38) * hybrid_goal_pmf(ag, ax, disp, nb_weight=0.38)
    if knockout:
        for k in list(g):
            if k[0] == k[1]: g[k] *= 0.8
    s = sum(g.values()); return {k: v/s for k, v in g.items()}, hx, ax


def wdl(g):
    return (sum(p for (x,y),p in g.items() if x>y), sum(p for (x,y),p in g.items() if x==y),
            sum(p for (x,y),p in g.items() if x<y))


def ah(g, line, side):
    w = p = 0
    for (hg, ag), pr in g.items():
        m = ((hg-ag) if side=='home' else (ag-hg)) + line
        if m > 0: w += pr
        elif m == 0: p += pr
    return w, p


def ah_quarter(g, line, side):
    def wp(L):
        w = p = 0
        for (hg, ag), pr in g.items():
            m = ((hg-ag) if side=='home' else (ag-hg)) + L
            if m > 0: w += pr
            elif m == 0: p += pr
        return w, p
    w1, p1 = wp(line-0.25); w2, p2 = wp(line+0.25); return (w1+w2)/2, (p1+p2)/2


def tot(g, line, over):
    w = p = 0; integer = abs(line-round(line)) < 1e-9
    for (hg, ag), pr in g.items():
        t = hg+ag
        if over and t > line: w += pr
        elif over and integer and t == line: p += pr
        elif (not over) and t < line: w += pr
        elif (not over) and integer and t == line: p += pr
    return w, p


def ev(win, push, odds, integer=True): return (win*odds+push-1) if integer else (win*odds-1)


def recommend_handicap(pw, pd, pl):
    """根据模型胜平负，给出建议的亚洲让盘方向。
    返回 (favside 'home'/'away', line 负数=让, label_suffix)。
    优势越大让得越深；接近局则给受让/防平。"""
    if pw >= pl:
        side, fav = 'home', pw
    else:
        side, fav = 'away', pl
    if fav >= 0.72:
        line = -1.5
    elif fav >= 0.60:
        line = -1.0
    elif fav >= 0.50:
        line = -0.5
    else:
        # 接近局：受让半球更稳（强方仍是 side，但建议 +0.5 防平）
        line = 0.5
    return side, line


def settle_handicap(side, line, fh, fa):
    """实际让盘结果：让赢/走水/让输（从 side 视角）。"""
    margin = (fh - fa) if side == 'home' else (fa - fh)
    m = margin + line
    if m > 0: return 'win'
    if m == 0: return 'push'
    return 'lose'


def settle_direction(rec, fh, fa):
    """结算预测方向（模型胜平负）。返回 ('hit'/'miss', 是否命中bool)。"""
    adir = 'H' if fh > fa else ('D' if fh == fa else 'A')
    hit = (rec.get('pred_dir') == adir)
    return ('hit' if hit else 'miss'), hit


def model_ah_pick(g, ah):
    """给定亚洲让分盘 ah={'home':[['-1',odds]...],'away':[['+1',odds]...]}，
    返回模型在该盘口选择的方向：(pick_side 'home'/'away', line_label, cover_prob)。
    用主队让分线计算双方打出概率，选概率高的一边。拿不到则返回 None。"""
    if not ah:
        return None
    home_lines = ah.get('home', [])
    away_lines = ah.get('away', [])
    if not home_lines and not away_lines:
        return None
    if home_lines:
        hl_label = home_lines[0][0]
        hl = parse_line(hl_label)
    else:
        al_label = away_lines[0][0]
        hl = -parse_line(al_label)
        hl_label = f"{hl:+g}"
    def cover(line, side):
        w = p = 0
        for (x, y), pr in g.items():
            m = ((x - y) if side == 'home' else (y - x)) + line
            if m > 0: w += pr
            elif m == 0: p += pr
        return w + 0.5 * p
    home_cover = cover(hl, 'home')
    away_cover = cover(-hl, 'away')
    if home_cover >= away_cover:
        lbl = home_lines[0][0] if home_lines else f"{hl:+g}"
        return ('home', lbl, home_cover)
    else:
        lbl = away_lines[0][0] if away_lines else f"{-hl:+g}"
        return ('away', lbl, away_cover)


def _load_hcap_snapshot():
    try:
        return json.load(open(os.path.join(BASE, 'handicap_snapshot.json'), encoding='utf-8')).get('handicap', {})
    except Exception:
        return {}


def hcap_for(r, hcap=None):
    """查该场让球盘线。返回 (home_point, away_point) 或 (None, None)。"""
    if hcap is None:
        hcap = _load_hcap_snapshot()
    key = f"{re.sub(r'[^a-z]', '', str(r['home']['source_name']).lower())}|" \
          f"{re.sub(r'[^a-z]', '', str(r['away']['source_name']).lower())}"
    info = hcap.get(key)
    if not info:
        return None, None
    return info.get('home_point'), info.get('away_point')


def build_prediction(r, ah=None):
    """为单场比赛生成预测字典（用于锁存）。
    预测方向 = 模型自己的胜平负判断（独立预测，不看盘口）。
    让球盘线只作赛后对照/回测，不参与方向生成。"""
    g, hx, ax = grid(r['home'], r['away'], is_knockout(r))
    top = sorted(g.items(), key=lambda x: x[1], reverse=True)[:2]
    pw, pd, pl = wdl(g)
    wdl_dir = 'H' if pw == max(pw, pd, pl) else ('D' if pd == max(pw, pd, pl) else 'A')
    if wdl_dir == 'H':
        dir_label = f"{cn(r['home']['name'])} 胜"
    elif wdl_dir == 'A':
        dir_label = f"{cn(r['away']['name'])} 胜"
    else:
        dir_label = "平"
    # 让球盘：冻结时记录模型方向对应的让球线（有盘口才存），供赛后回测让球命中
    hp, ap = hcap_for(r)
    hcap_side = hcap_line = None
    if hp is not None:
        if wdl_dir == 'H':
            hcap_side, hcap_line = 'home', hp
        elif wdl_dir == 'A':
            hcap_side, hcap_line = 'away', (ap if ap is not None else -hp)
        else:  # 模型看平：取盘口被让方（负分方）
            if hp <= 0:
                hcap_side, hcap_line = 'home', hp
            else:
                hcap_side, hcap_line = 'away', (ap if ap is not None else -hp)
    return dict(hxg=hx, axg=ax, pw=pw, pd=pd, pl=pl,
                top1=[top[0][0][0], top[0][0][1], top[0][1]],
                top2=[top[1][0][0], top[1][0][1], top[1][1]],
                pred_dir=wdl_dir, dir_label=dir_label,
                hcap_side=hcap_side, hcap_line=hcap_line,
                cn_home=cn(r['home']['name']), cn_away=cn(r['away']['name']))


def parse_line(s):
    """'2,2.5'->2.25 (quarter), '2'->2.0, '-2'-> -2.0"""
    parts = [float(x) for x in str(s).split(',')]
    return sum(parts)/len(parts)


def pick_secondary(g, pw, pd, pl):
    """次选比分：中等优势/接近局→防冷(最高概率平局比分)；否则→第二概率比分。
    返回 (h, a, prob, kind) kind ∈ {'防冷','次概率'}。"""
    ranked = sorted(g.items(), key=lambda x: x[1], reverse=True)
    top1 = ranked[0][0]
    fav = max(pw, pl)
    close = (fav < 0.60) or (pd >= 0.22)  # 中等优势或平局概率偏高
    if close:
        draws = [((h, a), p) for (h, a), p in ranked if h == a and (h, a) != top1]
        if draws:
            (h, a), p = draws[0]
            return h, a, p, '防冷'
    for (h, a), p in ranked[1:]:
        return h, a, p, '次概率'
    return top1[0], top1[1], ranked[0][1], '次概率'


# ---------- 回测（读取冻结的预测，不重算）----------
def backtest(rows):
    played = [r for r in rows if r.get('score') and r['score'].get('ft')]
    # 锁存命中率仅统计赛前冻结的（retro=False）；回溯补录的单独标注
    n = dirhit = e1 = eany = draws = dir_win = dir_settled = 0
    locked_n = locked_dir_win = locked_dir_settled = 0
    hcap_win = hcap_settled = 0   # 让球盘命中（仅统计有冻结让球线的完赛场，走水不计入分母）
    recent = []
    for r in played:
        rec = pstore.get(r)
        if not rec:
            continue  # 没有冻结预测则跳过
        fh, fa = r['score']['ft']
        adir = 'H' if fh > fa else ('D' if fh == fa else 'A')
        t1 = tuple(rec['top1'][:2]); t2 = tuple(rec['top2'][:2])
        dh = rec['pred_dir'] == adir
        ex1 = t1 == (fh, fa); exany = ex1 or t2 == (fh, fa)
        dres, dwin = settle_direction(rec, fh, fa)
        retro = rec.get('retro', False)
        n += 1; dirhit += dh; e1 += ex1; eany += exany; draws += (adir == 'D')
        if dres != 'push':
            dir_settled += 1; dir_win += dwin
            if not retro:
                locked_dir_settled += 1; locked_dir_win += dwin
        if not retro:
            locked_n += 1
        hcap_res = None
        if rec.get('hcap_side') and rec.get('hcap_line') is not None:
            hres = settle_handicap(rec['hcap_side'], rec['hcap_line'], fh, fa)
            if hres != 'push':
                hcap_settled += 1; hcap_win += (hres == 'win')
            hcap_res = hres
        recent.append(dict(home=rec['home'], away=rec['away'],
                           date=rec['date'], fh=fh, fa=fa,
                           pred=f"{rec['top1'][0]}-{rec['top1'][1]}", p=rec['top1'][2],
                           dir_ok=dh, exact=ex1,
                           dir_label=rec.get('dir_label', ''),
                           dres=dres, retro=retro, hcap_res=hcap_res,
                           hcap_side=rec.get('hcap_side'), hcap_line=rec.get('hcap_line')))
    return dict(n=n, dirhit=dirhit, e1=e1, eany=eany, draws=draws,
                dir_win=dir_win, dir_settled=dir_settled,
                locked_n=locked_n, locked_dir_win=locked_dir_win,
                locked_dir_settled=locked_dir_settled,
                hcap_win=hcap_win, hcap_settled=hcap_settled,
                recent=recent)
