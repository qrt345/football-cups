#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
世界杯预测看板 — HTML 生成器（自动更新用）

生成 dist/index.html，含三大板块：
  ① 历史预测准确度（KPI看板 + 比例条）
  ② 最新比分 / 昨日预测vs实际差异 / 接下来预测
  ③ 我的预测方向 × 韦德盘口 EV 对比（基于 odds_snapshot.json，标注赔率时间）

数据：
  - openfootball worldcup.json（赛果，多镜像重试）
  - fixtures_online_latest.json（赛程+Elo+攻防+近期进失球缓存）
  - odds_snapshot.json（韦德赔率快照，赛前更新）
独立可运行：python generate_dashboard.py
"""
from __future__ import annotations
import json, os, sys, math, html, ssl, urllib.request, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)
from score_predictor import expected_goals, hybrid_goal_pmf, Team
import predictions_store as pstore

DIST = os.path.join(BASE, "dist")
os.makedirs(DIST, exist_ok=True)

NAME = {'Uzbekistan': '乌兹别克斯坦', 'Panama': '巴拿马', 'DR Congo': '民主刚果',
        'Ghana': '加纳', 'Croatia': '克罗地亚', 'Portugal': '葡萄牙',
        'England': '英格兰', 'Colombia': '哥伦比亚'}
def cn(x): return NAME.get(x, x)


# ---------- 数据刷新 ----------
def fetch_worldcup():
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    urls = [
        "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json",
        "https://cdn.jsdelivr.net/gh/openfootball/worldcup.json@master/2026/worldcup.json",
        "https://fastly.jsdelivr.net/gh/openfootball/worldcup.json@master/2026/worldcup.json",
        "https://gcore.jsdelivr.net/gh/openfootball/worldcup.json@master/2026/worldcup.json",
    ]
    for u in urls:
        for _ in range(3):
            try:
                req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
                    return json.loads(r.read().decode('utf-8')).get('matches', [])
            except Exception:
                continue
    return None


def merge_scores(rows, matches):
    if not matches: return 0
    wc = {(m['team1'], m['team2'], m['date']): m['score']
          for m in matches if m.get('score') and m['score'].get('ft')}
    upd = 0
    for r in rows:
        if r.get('score'): continue
        hs, as_, d0 = r['home']['source_name'], r['away']['source_name'], r['kickoff'][:10]
        for d in [d0, (dt.date.fromisoformat(d0) - dt.timedelta(days=1)).isoformat()]:
            if (hs, as_, d) in wc:
                r['score'] = wc[(hs, as_, d)]; upd += 1; break
    return upd


# ---------- 模型 ----------
def mk(td): return Team(name=td['name'], rating=td['rating'], attack=td['attack'], defense=td['defense'],
                        recent_goals_for=td['recent_goals_for'], recent_goals_against=td['recent_goals_against'])
def adjxg(h, a, hx, ax):
    gap = abs(h.rating - a.rating); ratio = max(hx, ax) / max(0.01, min(hx, ax)); sh = hx >= ax
    if gap >= 400 or ratio >= 3.5: sf, wf = 0.97, 1.04
    elif gap >= 180 or ratio >= 2.0: sf, wf = 0.93, 1.06
    else: sf, wf = 0.96, 1.04
    return (hx*sf, ax*wf) if sh else (hx*wf, ax*sf)
def grid(home, away):
    h, a = mk(home), mk(away); hx, ax = expected_goals(h, a); hx, ax = adjxg(h, a, hx, ax)
    g = {}
    for hg in range(9):
        for ag in range(9): g[(hg, ag)] = hybrid_goal_pmf(hg, hx, 7.5) * hybrid_goal_pmf(ag, ax, 7.5)
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
    # 取主队侧的盘口线（可能是 '-1' / '+1' / '-2,-2.5' 等）
    home_lines = ah.get('home', [])
    away_lines = ah.get('away', [])
    if not home_lines and not away_lines:
        return None
    # 用主队让分线判定（home line 为准；若只有 away 线，用其相反数）
    if home_lines:
        hl_label = home_lines[0][0]
        hl = parse_line(hl_label)
    else:
        al_label = away_lines[0][0]
        hl = -parse_line(al_label)
        hl_label = f"{hl:+g}"
    # 主队让分线 hl（负=让），计算主队 cover 概率（走水按 0.5 计入比较）
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


def build_prediction(r, ah=None):
    """为单场比赛生成预测字典（用于锁存）。
    预测方向 = 模型自己的胜平负判断（独立预测，不看盘口）。
    韦德赔率只用于赛后对照，不参与方向生成。"""
    g, hx, ax = grid(r['home'], r['away'])
    top = sorted(g.items(), key=lambda x: x[1], reverse=True)[:2]
    pw, pd, pl = wdl(g)
    wdl_dir = 'H' if pw == max(pw, pd, pl) else ('D' if pd == max(pw, pd, pl) else 'A')
    if wdl_dir == 'H':
        dir_label = f"{cn(r['home']['name'])} 胜"
    elif wdl_dir == 'A':
        dir_label = f"{cn(r['away']['name'])} 胜"
    else:
        dir_label = "平"
    return dict(hxg=hx, axg=ax, pw=pw, pd=pd, pl=pl,
                top1=[top[0][0][0], top[0][0][1], top[0][1]],
                top2=[top[1][0][0], top[1][0][1], top[1][1]],
                pred_dir=wdl_dir, dir_label=dir_label,
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
    # 第二概率比分
    for (h, a), p in ranked[1:]:
        return h, a, p, '次概率'
    return top1[0], top1[1], ranked[0][1], '次概率'


# ---------- 回测（读取冻结的预测，不重算）----------
def backtest(rows):
    played = [r for r in rows if r.get('score') and r['score'].get('ft')]
    # 锁存命中率仅统计赛前冻结的（retro=False）；回溯补录的单独标注
    n = dirhit = e1 = eany = draws = dir_win = dir_settled = 0
    locked_n = locked_dir_win = locked_dir_settled = 0
    recent = []
    for r in played:
        rec = pstore.get(r)
        if not rec:
            continue  # 没有冻结预测则跳过
        fh, fa = r['score']['ft']
        adir = 'H' if fh > fa else ('D' if fh == fa else 'A')
        t1 = tuple(rec['top1'][:2]); t2 = tuple(rec['top2'][:2])
        # 胜平负方向（板块①的总方向命中率仍用这个）
        dh = rec['pred_dir'] == adir
        ex1 = t1 == (fh, fa); exany = ex1 or t2 == (fh, fa)
        # 预测方向（让盘优先 / 退回胜平负）
        dres, dwin = settle_direction(rec, fh, fa)
        retro = rec.get('retro', False)
        n += 1; dirhit += dh; e1 += ex1; eany += exany; draws += (adir == 'D')
        # dir_settled 排除走水
        if dres != 'push':
            dir_settled += 1; dir_win += dwin
            if not retro:
                locked_dir_settled += 1; locked_dir_win += dwin
        if not retro:
            locked_n += 1
        recent.append(dict(home=rec['home'], away=rec['away'],
                           date=rec['date'], fh=fh, fa=fa,
                           pred=f"{rec['top1'][0]}-{rec['top1'][1]}", p=rec['top1'][2],
                           dir_ok=dh, exact=ex1,
                           dir_label=rec.get('dir_label', ''),
                           dres=dres, retro=retro))
    return dict(n=n, dirhit=dirhit, e1=e1, eany=eany, draws=draws,
                dir_win=dir_win, dir_settled=dir_settled,
                locked_n=locked_n, locked_dir_win=locked_dir_win,
                locked_dir_settled=locked_dir_settled,
                recent=recent)


# ---------- HTML 渲染 ----------
def bar(pct, color):
    return (f'<div class="bar"><div class="bar-fill" style="width:{pct:.0f}%;background:{color}"></div>'
            f'<span class="bar-label">{pct:.0f}%</span></div>')

def evcls(e):
    if e is None: return ""
    return "pos" if e > 0 else "neg"


CSS_HEAD = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>世界杯预测看板</title>
<style>
:root{--bg:#f4f5f8;--card:#fff;--bd:#e7e9f0;--tx:#1b2030;--mut:#646b7d;--dim:#9aa0ad;
--info-bg:#E6F1FB;--info-tx:#185FA5;--ok-bg:#E1F5EE;--ok-tx:#0F6E56;--warn-bg:#FAEEDA;--warn-tx:#854F0B;
--bad-bg:#FCEBEB;--bad-tx:#A32D2D;--mc-bg:#F1EFE8;--mc-tx:#5F5E5A;--home:#378ADD;--draw:#EF9F27;--away:#B4B2A9}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--tx);line-height:1.5;padding:18px 14px}
.wrap{max-width:880px;margin:0 auto}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px}
.mut{color:var(--mut)}.dim{color:var(--dim)}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:15px 17px;margin-bottom:13px}
.sec{font-size:15px;font-weight:600;margin:20px 0 10px;border-left:3px solid var(--info-tx);padding-left:9px}
.chip{display:inline-block;padding:1px 8px;border-radius:7px;font-size:12px;font-weight:600;white-space:nowrap}
.ok{background:var(--ok-bg);color:var(--ok-tx)}.warn{background:var(--warn-bg);color:var(--warn-tx)}
.info{background:var(--info-bg);color:var(--info-tx)}.bad{background:var(--bad-bg);color:var(--bad-tx)}.mc{background:var(--mc-bg);color:var(--mc-tx)}
.wdl{display:flex;height:7px;border-radius:4px;overflow:hidden}.wdl span{display:block}
.slate{display:flex;align-items:center;gap:11px;padding:10px 0;border-bottom:1px solid var(--bd)}
.slate:last-child{border-bottom:none}
.tile{background:var(--bg);border-radius:9px;padding:11px;text-align:center}
.tnum{font-size:21px;font-weight:700;line-height:1.1}
.pa{font-variant-numeric:tabular-nums;white-space:nowrap;font-size:13px}
.vrow{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--bd)}
.vrow:last-child{border-bottom:none}
.pp{width:42px;text-align:right;font-weight:600;font-size:13px;font-variant-numeric:tabular-nums}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
input[type=range]{-webkit-appearance:none;appearance:none;height:5px;border-radius:3px;background:var(--bd);outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:18px;height:18px;border-radius:50%;background:var(--info-tx);cursor:pointer}
input[type=range]::-moz-range-thumb{width:18px;height:18px;border:none;border-radius:50%;background:var(--info-tx);cursor:pointer}
.note{font-size:12px;color:var(--mut);margin-top:8px;line-height:1.5}
.foot{font-size:11px;color:var(--dim);text-align:center;margin:18px 0 4px}
</style></head><body><div class="wrap">"""


RADAR_JS = r"""
function evcss(e){return e>0?'var(--ok-tx)':'var(--bad-tx)';}
function dotc(p){return 'var(--'+(p>=70?'ok-tx':(p>=50?'info-tx':'warn-tx'))+')';}
R.sort(function(a,b){var pa=(a.ev!=null&&a.ev>0),pb=(b.ev!=null&&b.ev>0);
 if(pa!=pb)return pa?-1:1; if(pa&&pb)return b.ev-a.ev; return b.p-a.p;});
var sld=document.getElementById('sld'),out=document.getElementById('out'),
    list=document.getElementById('list'),cnt=document.getElementById('cnt');
function draw(){var t=parseInt(sld.value);out.textContent=t+'%';
 var rows=R.filter(function(o){return o.p>=t;});
 list.innerHTML=rows.map(function(o){
  var right='<span style="font-size:12px;font-weight:600;color:'+evcss(o.ev)+'">EV '+(o.ev>0?'+':'')+o.ev+'%</span>';
  return '<div class="vrow"><span class="pp">'+o.p+'%</span>'+
   '<span class="dot" style="background:'+dotc(o.p)+'"></span>'+
   '<span style="flex:1;font-size:13px">'+o.l+' <span class="dim" style="font-size:11px">'+o.m+'</span></span>'+right+'</div>';
 }).join('')||'<div class="dim" style="padding:10px 0;font-size:12px">该阈值下无买法</div>';
 cnt.textContent=rows.length+' 项 / 共 '+R.length;}
sld.addEventListener('input',draw);draw();
"""


def render(rows, src, upd):
    now = dt.datetime.now().astimezone()
    bt = backtest(rows)
    n = max(1, bt['n'])
    snap = {}
    try: snap = json.load(open('odds_snapshot.json', encoding='utf-8'))
    except Exception: pass
    odds = snap.get('odds', {}); odds_time = snap.get('updated_at', '未知')

    # featured day = 最早还有未开赛场次的那天（剔除已开球但结果未落库的场，避免把进行中的比赛当未来场）
    def _kdt(r):
        try: return dt.datetime.fromisoformat(r['kickoff'])
        except Exception: return None
    unp = sorted([r for r in rows if not r.get('score') and (_kdt(r) is None or _kdt(r) > now)],
                 key=lambda r: r['kickoff'])
    next_date = unp[0]['kickoff'][:10] if unp else None
    daybatch = [r for r in unp if r['kickoff'][:10] == next_date] if next_date else []

    # 上一批战绩 = 最近一个有结果的日期的全部完赛场（从锁存回测取，含预测比分→实际）
    recent = bt['recent']
    last_date = max((x['date'] for x in recent), default=None)
    lastbatch = [x for x in recent if x['date'] == last_date] if last_date else []

    def wdlbar(pw, pd, pl):
        return ('<div class="wdl"><span style="width:%.0f%%;background:var(--home)"></span>'
                '<span style="width:%.0f%%;background:var(--draw)"></span>'
                '<span style="width:%.0f%%;background:var(--away)"></span></div>') % (pw*100, pd*100, pl*100)

    # ---- 价值雷达：只遍历韦德实际有的让分线+大小线，每条算模型概率，保留模型看好(≥50%)的那一边 ----
    # 这样每一行都必然有盘口赔率+EV，不再凭空生成无赔率的买法。
    radar = []
    for r in daybatch:
        key = f"{r['home']['source_name']}|{r['away']['source_name']}"
        o = odds.get(key)
        g, hx, ax = grid(r['home'], r['away'])
        hn, an = cn(r['home']['name']), cn(r['away']['name']); mt = f"{hn} vs {an}"
        if not o:
            continue  # 没盘口的场次直接跳过（不显示"赔率待更新"）
        def add(lbl, prob, oddval, push=0.0, integer=True):
            if prob < 0.50 or not oddval:
                return  # 只收录：模型概率≥50% 且 有真实赔率
            e = ev(prob, push, oddval, integer)
            radar.append({"m": mt, "l": lbl, "p": round(prob*100),
                          "ev": round(e*1000)/10})
        # 让分盘：主客两边都算，保留模型概率≥50%那边
        for sidekey, side, nm in [('home', 'home', hn), ('away', 'away', an)]:
            for ln, od in o.get('ah', {}).get(sidekey, []):
                line = parse_line(ln)
                if abs(line*4-round(line*4)) < 1e-9 and abs(line*2-round(line*2)) > 1e-9:
                    w, p = ah_quarter(g, line, side); integer = False
                else:
                    w, p = ah(g, line, side); integer = abs(line-round(line)) < 1e-9
                add(f"{nm} 让{ln}", w, od, p, integer)
        # 大小盘：大/小两边都算，保留模型概率≥50%那边
        for ln, od in o.get('totals', {}).get('over', []):
            L = parse_line(ln); w, p = tot(g, L, True)
            add(f"大{ln}球", w, od, p, abs(L-round(L)) < 1e-9)
        for ln, od in o.get('totals', {}).get('under', []):
            L = parse_line(ln); w, p = tot(g, L, False)
            add(f"小{ln}球", w, od, p, abs(L-round(L)) < 1e-9)
    radar.sort(key=lambda x: -x['ev'])
    radar_json = json.dumps(radar, ensure_ascii=False)

    H = [CSS_HEAD]
    # 顶栏
    H.append(f'<div class="row"><div style="font-size:18px;font-weight:700">🏆 世界杯预测看板</div>'
             f'<div style="display:flex;gap:6px">'
             f'<span class="chip mc">数据 {now.strftime("%m-%d %H:%M")}</span>'
             f'<span class="chip warn">赔率 {odds_time}</span></div></div>')
    H.append(f'<div class="dim" style="font-size:12px;margin-top:5px">数据源：{src} · 本次新结算 {upd} 场 · 北京时间</div>')

    # ① 接下来 · 当天整批
    if daybatch:
        hero = daybatch[0]; rest = daybatch[1:]
        g, hx, ax = grid(hero['home'], hero['away']); pw, pd, pl = wdl(g)
        top = sorted(g.items(), key=lambda x: x[1], reverse=True)[0]
        sh, sa, sp, skind = pick_secondary(g, pw, pd, pl)
        hn, an = cn(hero['home']['name']), cn(hero['away']['name'])
        sig = [x for x in radar if x['m'] == f"{hn} vs {an}" and x['ev'] is not None]
        if sig:
            best = max(sig, key=lambda x: x['ev'])
            if best['ev'] > 0:
                sigchip = f'<span class="chip ok">有价值 · {best["l"]}</span>'
                signote = f'最优 {best["l"]}：EV <b style="color:var(--ok-tx)">+{best["ev"]:.1f}%</b>'
            else:
                sigchip = '<span class="chip warn">无价值 · 观望</span>'
                signote = f'最优 {best["l"]}：EV <b style="color:var(--bad-tx)">{best["ev"]:.1f}%</b>，被盘口压价'
        else:
            sigchip = '<span class="chip mc">赔率待更新</span>'
            signote = '暂无该场韦德盘口，刷新后给出 EV 判定'
        H.append(f'<div class="sec" style="margin-bottom:8px">{next_date} · 全天 {len(daybatch)} 场</div>')
        H.append('<div class="card" style="border:2px solid var(--info-tx)">')
        H.append(f'<div class="row"><span class="chip info">下一场 · {hero["kickoff"][11:16]}</span>'
                 f'<span class="mut" style="font-size:12px">本日第 1 / {len(daybatch)} 场</span></div>')
        H.append(f'<div style="font-size:21px;font-weight:700;margin:8px 0 2px">{hn} '
                 f'<span class="dim" style="font-size:15px">vs</span> {an}</div>')
        H.append('<div style="display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-top:11px;align-items:center">')
        H.append(f'<div><div class="mut" style="font-size:12px;margin-bottom:4px">预期进球 xG</div>'
                 f'<div style="font-size:19px;font-weight:600">{hx:.2f} <span class="dim" style="font-size:13px">—</span> {ax:.2f}</div>'
                 f'<div style="margin-top:9px">{wdlbar(pw, pd, pl)}</div>'
                 f'<div class="row" style="margin-top:4px;font-size:11px"><span style="color:var(--info-tx)">主胜 {pw*100:.0f}</span>'
                 f'<span style="color:var(--warn-tx)">平 {pd*100:.0f}</span><span class="mut">客胜 {pl*100:.0f}</span></div></div>')
        H.append(f'<div style="border-left:1px solid var(--bd);padding-left:15px">'
                 f'<div class="mut" style="font-size:12px">模型首选比分</div>'
                 f'<div style="font-size:29px;font-weight:700;line-height:1.1">{top[0][0]}-{top[0][1]}</div>'
                 f'<div class="mut" style="font-size:12px;margin-top:1px">{top[1]*100:.0f}% · 次选 {sh}-{sa} '
                 f'<span class="chip warn">{skind}</span></div></div>')
        H.append('</div>')
        H.append(f'<div class="row" style="margin-top:12px;padding-top:11px;border-top:1px solid var(--bd)">'
                 f'<span style="font-size:12px"><span class="mut">盘口信号</span> {signote}</span>{sigchip}</div>')
        H.append('</div>')
        if rest:
            H.append('<div class="sec">当天其余 %d 场</div><div class="card" style="padding:2px 17px">' % len(rest))
            for r in rest:
                g2, hx2, ax2 = grid(r['home'], r['away']); pw2, pd2, pl2 = wdl(g2)
                top2 = sorted(g2.items(), key=lambda x: x[1], reverse=True)[0]
                sh2, sa2, sp2, sk2 = pick_secondary(g2, pw2, pd2, pl2)
                hn2, an2 = cn(r['home']['name']), cn(r['away']['name'])
                favp = max(pw2, pl2)
                favchip = 'ok' if favp >= 0.60 else ('info' if favp >= 0.45 else 'mc')
                H.append(f'<div class="slate"><span class="mut" style="width:42px;font-size:12px">{r["kickoff"][11:16]}</span>'
                         f'<div style="flex:1"><div style="font-weight:600">{hn2} vs {an2}</div>'
                         f'<div class="dim" style="font-size:11px">xG {hx2:.2f}-{ax2:.2f} · 次选 {sh2}-{sa2} {sk2}</div></div>'
                         f'<div style="width:80px">{wdlbar(pw2, pd2, pl2)}</div>'
                         f'<span class="chip {favchip}" style="width:52px;text-align:center">{top2[0][0]}-{top2[0][1]}</span></div>')
            H.append('</div>')
    else:
        H.append('<div class="sec">接下来</div><div class="card"><div class="mut">'
                 '当前没有即将开赛的场次，赛程稍后更新。</div></div>')

    # ② 上一批战绩（预测比分 → 实际比分）
    if lastbatch:
        H.append(f'<div class="sec">上一批战绩 · {last_date[5:]}</div><div class="card" style="padding:2px 17px">')
        for x in lastbatch:
            if x['exact']: prec = '<span class="chip ok">精确✓</span>'
            elif x['dir_ok']: prec = '<span class="chip warn">方向对</span>'
            else: prec = '<span class="chip bad">未中</span>'
            dres = x.get('dres')
            if dres == 'hit': dchip = '<span class="chip ok">方向命中</span>'
            else: dchip = '<span class="chip bad">方向未中</span>'
            lock = '回溯' if x.get('retro') else '赛前锁存'
            H.append(f'<div class="slate"><div style="flex:1"><div style="font-weight:600">{x["home"]} vs {x["away"]}</div>'
                     f'<div class="dim" style="font-size:11px">预测方向：{x.get("dir_label", "")} · {lock}</div></div>'
                     f'<span class="pa"><span class="mut">预测</span> {x["pred"]} → <span style="font-weight:600">实际 {x["fh"]}-{x["fa"]}</span></span>'
                     f'{prec}{dchip}</div>')
        H.append('<div class="note">每行：模型预测比分 → 实际比分。比分精度 = 精确 / 方向对 / 未中；预测方向 = 模型胜平负判断是否命中。</div></div>')

    # ③ 模型战绩
    H.append(f'<div class="sec">模型战绩 · 回测 {bt["n"]} 场</div>')
    ds = max(1, bt['dir_settled'])
    H.append('<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:9px">')
    H.append(f'<div class="tile"><div class="tnum" style="color:var(--info-tx)">{bt["dir_win"]/ds*100:.0f}%</div>'
             f'<div class="mut" style="font-size:11px;margin-top:2px">方向命中 {bt["dir_win"]}/{bt["dir_settled"]}</div></div>')
    H.append(f'<div class="tile"><div class="tnum">{bt["e1"]/n*100:.0f}%</div>'
             f'<div class="mut" style="font-size:11px;margin-top:2px">Top1精确 {bt["e1"]}/{bt["n"]}</div></div>')
    H.append(f'<div class="tile"><div class="tnum">{bt["eany"]/n*100:.0f}%</div>'
             f'<div class="mut" style="font-size:11px;margin-top:2px">Top2任一 {bt["eany"]}/{bt["n"]}</div></div>')
    H.append(f'<div class="tile"><div class="tnum" style="color:var(--warn-tx)">{bt["draws"]/n*100:.0f}%</div>'
             f'<div class="mut" style="font-size:11px;margin-top:2px">实际平局率</div></div>')
    H.append('</div>')

    # ④ 价值雷达（滑块按概率筛选）
    H.append('<div class="sec">价值雷达 · 拖动按概率筛选</div><div class="card">')
    H.append('<div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">'
             '<label class="mut" style="font-size:13px;white-space:nowrap">模型概率 ≥</label>'
             '<input type="range" min="50" max="90" value="50" step="5" id="sld" style="flex:1">'
             '<span style="font-size:15px;font-weight:700;min-width:42px" id="out">50%</span></div>')
    H.append(f'<div class="row" style="margin-bottom:4px"><span class="dim" style="font-size:11px" id="cnt"></span>'
             f'<span class="dim" style="font-size:11px">{next_date or ""} · 各买法</span></div>')
    H.append('<div id="list"></div>')
    H.append(f'<div class="note">只列出「模型概率≥50% 且 韦德有对应让分/大小盘」的买法，每条都带 EV，正EV置顶。'
             f'概率高 ≠ 有价值：高概率项常被盘口压价，乘上赔率算出正EV 才叫有价值。赔率快照 {odds_time}。</div></div>')
    H.append('<div class="foot">数据：openfootball + World Football Elo Ratings；赔率：BetVictor 快照。'
             '本页脚本自动生成，比赛结束后逐场更新。</div></div>')
    H.append('<script>var R=%s;\n%s</script></body></html>' % (radar_json, RADAR_JS))
    return "\n".join(H)


def main():
    rows = json.load(open('fixtures_online_latest.json', encoding='utf-8'))
    # 载入韦德让分盘快照，构造带赔率的预测函数
    odds = {}
    try:
        odds = json.load(open('odds_snapshot.json', encoding='utf-8')).get('odds', {})
    except Exception:
        pass
    def predict_fn(r):
        return build_prediction(r)  # 预测方向=模型胜平负，不依赖盘口
    # 1) 先冻结：对当前所有"未完赛"比赛锁存预测（赛前快照，retro=False）。
    #    必须在合并新比分之前做，确保刚结束的比赛用的是赛前冻结的预测。
    locked = pstore.freeze_pending(rows, predict_fn, retro_played=False)
    # 2) 合并最新比分
    matches = fetch_worldcup()
    upd = merge_scores(rows, matches)
    json.dump(rows, open('fixtures_online_latest.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    # 3) 回溯补录：已完赛但日志里没有的（历史场次），标记 retro=True，不计入锁存命中率
    retro = pstore.freeze_pending(rows, predict_fn, retro_played=True)
    src = "实时刷新" if matches else "本地缓存(联网失败)"
    print(f"[freeze] 赛前锁存 {locked} 场，回溯补录 {retro} 场")
    htmls = render(rows, src, upd)
    out = os.path.join(DIST, "index.html")
    open(out, "w", encoding="utf-8").write(htmls)
    # 记录已完赛集合（供变化检测）
    played_keys = sorted(f"{r['home']['source_name']}|{r['away']['source_name']}"
                         for r in rows if r.get('score') and r['score'].get('ft'))
    json.dump({"played": played_keys, "updated_at": dt.datetime.now().astimezone().isoformat()},
              open(os.path.join(BASE, "_published_state.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[done] 已生成 {out} ({len(htmls)} 字节)，新结算 {upd} 场，已完赛 {len(played_keys)} 场")
    return out


if __name__ == "__main__":
    main()
