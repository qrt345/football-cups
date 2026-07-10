#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
世界杯预测看板 — HTML 生成器（自动更新用）

生成 dist/index.html，含三大板块：
  ① 接下来 · 当天整批预测（hero 卡 + 当天其余场次）
  ② 上一批战绩（预测比分 → 实际比分）
  ③ 模型战绩（方向命中 / Top1精确 / Top2任一 / 实际平局率）

数据：
  - openfootball worldcup.json（赛果，多镜像重试）
  - fixtures_online_latest.json（赛程+Elo+攻防+近期进失球缓存）
分层：本文件是看板层（产品），只做数据刷新+渲染；
  模型计算全部在 prediction_model.py（模型预测层），本文件只调用不实现。
独立可运行：python generate_dashboard.py
"""
from __future__ import annotations
import json, os, sys, ssl, re, urllib.request, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)
import predictions_store as pstore
from prediction_model import (cn, grid, wdl, tot, is_knockout,
                              build_prediction, pick_secondary, backtest)

DIST = os.path.join(BASE, "dist")
os.makedirs(DIST, exist_ok=True)

# ---- 北京时间辅助函数（云端 runner 可能是 UTC，所有时间计算必须显式指定）----
BJT = dt.timezone(dt.timedelta(hours=8))

def bj_now():
    return dt.datetime.now(BJT)

def bj_time(iso_str):
    return dt.datetime.fromisoformat(iso_str).astimezone(BJT)

def bj_date(iso_str):
    return bj_time(iso_str).strftime('%Y-%m-%d')

def bj_hhmm(iso_str):
    return bj_time(iso_str).strftime('%H:%M')


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
        hs, as_, d0 = r['home']['source_name'], r['away']['source_name'], bj_date(r['kickoff'])
        for d in [d0, (dt.date.fromisoformat(d0) - dt.timedelta(days=1)).isoformat()]:
            if (hs, as_, d) in wc:
                r['score'] = wc[(hs, as_, d)]; upd += 1; break
    return upd


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
function dotc(p){return 'var(--'+(p>=70?'ok-tx':(p>=60?'info-tx':'warn-tx'))+')';}
R.sort(function(a,b){return b.p-a.p;});
var sld=document.getElementById('sld'),out=document.getElementById('out'),
    list=document.getElementById('list'),cnt=document.getElementById('cnt');
function draw(){var t=parseInt(sld.value);out.textContent=t+'%';
 var rows=R.filter(function(o){return o.p>=t;});
 list.innerHTML=rows.map(function(o){
  return '<div class="vrow"><span class="pp">'+o.p+'%</span>'+
   '<span class="dot" style="background:'+dotc(o.p)+'"></span>'+
   '<span style="flex:1;font-size:13px">'+o.l+' <span class="dim" style="font-size:11px">'+o.m+' · '+o.t+'</span></span></div>';
 }).join('')||'<div class="dim" style="padding:10px 0;font-size:12px">该阈值下无买法</div>';
 cnt.textContent=rows.length+' 项 / 共 '+R.length;}
sld.addEventListener('input',draw);draw();
"""


def render(rows, src, upd):
    now = bj_now()
    bt = backtest(rows)
    n = max(1, bt['n'])

    # 让球盘快照（the-odds-api spreads）：键=norm(home)|norm(away)，值含 home_point/away_point
    hcap = {}
    try:
        hcap = json.load(open('handicap_snapshot.json', encoding='utf-8')).get('handicap', {})
    except Exception:
        pass
    def _hnorm(s):
        return re.sub(r'[^a-z]', '', str(s).lower())
    def handicap_pick(r, pw, pd, pl, hn, an):
        """返回让球盘推荐文字：结合模型方向 + 让球盘线。无盘口返回 None。"""
        key = f"{_hnorm(r['home']['source_name'])}|{_hnorm(r['away']['source_name'])}"
        info = hcap.get(key)
        if not info:
            return None
        hp = info.get('home_point'); ap = info.get('away_point')
        if hp is None:
            return None
        mx = max(pw, pd, pl)
        # 模型方向那队 + 其让球线
        if pw == mx:      # 模型看好主队
            side, pt = hn, hp
        elif pl == mx:    # 模型看好客队
            side, pt = an, (ap if ap is not None else -hp)
        else:             # 模型看平：给盘口被让方（负分方）作参考
            if hp <= 0:
                side, pt = hn, hp
            else:
                side, pt = an, (ap if ap is not None else -hp)
        return f"{side} {pt:+g}"

    # featured day = 最早还有未开赛场次的那天（剔除已开球但结果未落库的场，避免把进行中的比赛当未来场）
    def _kdt(r):
        try: return bj_time(r['kickoff'])
        except Exception: return None
    unp = sorted([r for r in rows if not r.get('score') and (_kdt(r) is None or _kdt(r) > now)],
                 key=lambda r: r['kickoff'])
    # 批次规则：1/4决赛起按整轮出（8强4场 / 半决赛2场 / 决赛+季军一起）；之前按天出
    def stage_group(r):
        c = r.get('competition', '')
        if 'Quarter-final' in c: return '1/4决赛'
        if 'Semi-final' in c: return '半决赛'
        if 'third place' in c or 'Final' in c: return '决赛 · 季军赛'
        return None
    grp = stage_group(unp[0]) if unp else None
    if grp:
        daybatch = [r for r in unp if stage_group(r) == grp]
        next_date = None
        batch_title = f'{grp} · 共 {len(daybatch)} 场'
        seq_word = '本轮'
        batch_tag = grp
    else:
        next_date = bj_date(unp[0]['kickoff']) if unp else None
        daybatch = [r for r in unp if bj_date(r['kickoff']) == next_date] if next_date else []
        batch_title = f'{next_date} · 全天 {len(daybatch)} 场' if next_date else ''
        seq_word = '本日'
        batch_tag = next_date or ''
    # 整轮批次跨多天，时间要带日期
    tw = '78px' if grp else '42px'
    def ko_time(r):
        return bj_time(r['kickoff']).strftime('%m-%d %H:%M') if grp else bj_hhmm(r['kickoff'])

    # 上一批战绩 = 最近一个有结果的日期的全部完赛场（从锁存回测取，含预测比分→实际）
    recent = bt['recent']
    last_date = max((x['date'] for x in recent), default=None)
    lastbatch = [x for x in recent if x['date'] == last_date] if last_date else []

    def wdlbar(pw, pd, pl):
        return ('<div class="wdl"><span style="width:%.0f%%;background:var(--home)"></span>'
                '<span style="width:%.0f%%;background:var(--draw)"></span>'
                '<span style="width:%.0f%%;background:var(--away)"></span></div>') % (pw*100, pd*100, pl*100)

    # ---- 概率雷达：纯模型概率，无盘口。对当天每场算各类买法概率，保留 ≥50% 的，按概率降序 ----
    radar = []
    for r in daybatch:
        g, hx, ax = grid(r['home'], r['away'], is_knockout(r))
        pw, pd, pl = wdl(g)
        hn, an = cn(r['home']['name']), cn(r['away']['name']); mt = f"{hn} vs {an}"
        cand = []  # (label, prob)
        # 胜平负
        cand.append((f"{hn} 胜", pw))
        cand.append(("平局", pd))
        cand.append((f"{an} 胜", pl))
        # 不败（让+0.5，含平局）
        cand.append((f"{hn} 不败", pw + pd))
        cand.append((f"{an} 不败", pl + pd))
        # 大小球 2.5
        o25, _ = tot(g, 2.5, True); u25, _ = tot(g, 2.5, False)
        cand.append(("大 2.5 球", o25))
        cand.append(("小 2.5 球", u25))
        # 双方进球（BTTS）
        btts = sum(p for (x, y), p in g.items() if x >= 1 and y >= 1)
        cand.append(("双方进球", btts))
        for lbl, prob in cand:
            if prob >= 0.50:
                radar.append({"m": mt, "l": lbl, "p": round(prob * 100), "t": ko_time(r)})
    radar.sort(key=lambda x: -x['p'])

    H = [CSS_HEAD]
    # 顶栏
    H.append(f'<div class="row"><div style="font-size:18px;font-weight:700">🏆 世界杯预测看板</div>'
             f'<div style="display:flex;gap:6px">'
             f'<span class="chip mc">数据 {now.strftime("%m-%d %H:%M")}</span></div></div>')
    H.append(f'<div class="dim" style="font-size:12px;margin-top:5px">数据源：{src} · 本次新结算 {upd} 场 · 北京时间</div>')

    # 预测方向文字 + 大小球推荐（2.5 球线，取概率较大一边）
    def dir_text(hn, an, pw, pd, pl):
        mx = max(pw, pd, pl)
        if pw == mx: return f"{hn} 胜", pw
        if pl == mx: return f"{an} 胜", pl
        return "平局", pd
    def ou_pick(g):
        o25, _ = tot(g, 2.5, True); u25, _ = tot(g, 2.5, False)
        return ("大 2.5", o25) if o25 >= u25 else ("小 2.5", u25)

    # ① 接下来 · 当天整批
    if daybatch:
        hero = daybatch[0]; rest = daybatch[1:]
        g, hx, ax = grid(hero['home'], hero['away'], is_knockout(hero)); pw, pd, pl = wdl(g)
        top = sorted(g.items(), key=lambda x: x[1], reverse=True)[0]
        sh, sa, sp, skind = pick_secondary(g, pw, pd, pl)
        hn, an = cn(hero['home']['name']), cn(hero['away']['name'])
        dtxt, dprob = dir_text(hn, an, pw, pd, pl)
        oul, oup = ou_pick(g)
        hpk = handicap_pick(hero, pw, pd, pl, hn, an)
        H.append(f'<div class="sec" style="margin-bottom:8px">{batch_title}</div>')
        H.append('<div class="card" style="border:2px solid var(--info-tx)">')
        H.append(f'<div class="row"><span class="chip info">下一场 · {ko_time(hero)}</span>'
                 f'<span class="mut" style="font-size:12px">{seq_word}第 1 / {len(daybatch)} 场</span></div>')
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
        hpk_html = (f'<span style="font-size:12px"><span class="mut">让球盘</span> '
                    f'<b style="color:var(--warn-tx)">{hpk} 胜</b></span>') if hpk else \
                   '<span style="font-size:12px"><span class="mut">让球盘</span> <span class="dim">待更新</span></span>'
        H.append(f'<div class="row" style="margin-top:11px;padding-top:10px;border-top:1px solid var(--bd);gap:8px;flex-wrap:wrap">'
                 f'<span style="font-size:12px"><span class="mut">预测方向</span> '
                 f'<b style="color:var(--info-tx)">{dtxt}</b> <span class="dim">{dprob*100:.0f}%</span></span>'
                 f'<span style="font-size:12px"><span class="mut">大小球</span> '
                 f'<b style="color:var(--ok-tx)">{oul}</b> <span class="dim">{oup*100:.0f}%</span></span>'
                 f'{hpk_html}</div>')
        H.append('</div>')
        if rest:
            H.append(f'<div class="sec">{seq_word}其余 {len(rest)} 场</div><div class="card" style="padding:2px 17px">')
            for r in rest:
                g2, hx2, ax2 = grid(r['home'], r['away'], is_knockout(r)); pw2, pd2, pl2 = wdl(g2)
                top2 = sorted(g2.items(), key=lambda x: x[1], reverse=True)[0]
                sh2, sa2, sp2, sk2 = pick_secondary(g2, pw2, pd2, pl2)
                hn2, an2 = cn(r['home']['name']), cn(r['away']['name'])
                favp = max(pw2, pl2)
                favchip = 'ok' if favp >= 0.60 else ('info' if favp >= 0.45 else 'mc')
                dtxt2, dprob2 = dir_text(hn2, an2, pw2, pd2, pl2)
                oul2, oup2 = ou_pick(g2)
                hpk2 = handicap_pick(r, pw2, pd2, pl2, hn2, an2)
                hpk2_html = f' · <span class="mut">让球</span> <b style="color:var(--warn-tx)">{hpk2}</b>' if hpk2 else ''
                H.append(f'<div class="slate"><span class="mut" style="width:{tw};font-size:12px">{ko_time(r)}</span>'
                         f'<div style="flex:1"><div style="font-weight:600">{hn2} vs {an2}</div>'
                         f'<div class="dim" style="font-size:11px">xG {hx2:.2f}-{ax2:.2f} · 次选 {sh2}-{sa2} {sk2}</div>'
                         f'<div style="font-size:11px;margin-top:1px"><span class="mut">方向</span> '
                         f'<b style="color:var(--info-tx)">{dtxt2}</b> {dprob2*100:.0f}% · '
                         f'<span class="mut">大小</span> <b style="color:var(--ok-tx)">{oul2}</b> {oup2*100:.0f}%{hpk2_html}</div></div>'
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
            # 让球盘结果（冻结时有让球线才结算；走水不算输赢）
            hres = x.get('hcap_res')
            if hres == 'win': hchip = '<span class="chip ok">让盘✓</span>'
            elif hres == 'push': hchip = '<span class="chip mc">让盘走水</span>'
            elif hres == 'lose': hchip = '<span class="chip bad">让盘✗</span>'
            else: hchip = '<span class="chip mc">无让球线</span>'
            if x.get('hcap_side') and x.get('hcap_line') is not None:
                hteam = x['home'] if x['hcap_side'] == 'home' else x['away']
                hline_txt = f' · 让球线 {hteam} {x["hcap_line"]:+g}'
            else:
                hline_txt = ''
            lock = '回溯' if x.get('retro') else '赛前锁存'
            H.append(f'<div class="slate"><div style="flex:1"><div style="font-weight:600">{x["home"]} vs {x["away"]}</div>'
                     f'<div class="dim" style="font-size:11px">预测方向：{x.get("dir_label", "")}{hline_txt} · {lock}</div></div>'
                     f'<span class="pa"><span class="mut">预测</span> {x["pred"]} → <span style="font-weight:600">实际 {x["fh"]}-{x["fa"]}</span></span>'
                     f'{prec}{dchip}{hchip}</div>')
        H.append('<div class="note">每行：模型预测比分 → 实际比分。比分精度 = 精确 / 方向对 / 未中；'
                 '预测方向 = 模型胜平负判断是否命中；让盘 = 冻结让球线的过盘结果（✓赢 / ✗输 / 走水，赛前无盘口则标无让球线）。</div></div>')

    # ③ 模型战绩
    H.append(f'<div class="sec">模型战绩 · 回测 {bt["n"]} 场</div>')
    ds = max(1, bt['dir_settled'])
    hs = bt.get('hcap_settled', 0); hw = bt.get('hcap_win', 0)
    hcap_pct = f'{hw/hs*100:.0f}%' if hs else '—'
    hcap_sub = f'让球盘命中 {hw}/{hs}' if hs else '让球盘命中 暂无'
    H.append('<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:9px">')
    H.append(f'<div class="tile"><div class="tnum" style="color:var(--info-tx)">{bt["dir_win"]/ds*100:.0f}%</div>'
             f'<div class="mut" style="font-size:11px;margin-top:2px">方向命中 {bt["dir_win"]}/{bt["dir_settled"]}</div></div>')
    H.append(f'<div class="tile"><div class="tnum" style="color:var(--warn-tx)">{hcap_pct}</div>'
             f'<div class="mut" style="font-size:11px;margin-top:2px">{hcap_sub}</div></div>')
    H.append(f'<div class="tile"><div class="tnum">{bt["e1"]/n*100:.0f}%</div>'
             f'<div class="mut" style="font-size:11px;margin-top:2px">Top1精确 {bt["e1"]}/{bt["n"]}</div></div>')
    H.append(f'<div class="tile"><div class="tnum">{bt["eany"]/n*100:.0f}%</div>'
             f'<div class="mut" style="font-size:11px;margin-top:2px">Top2任一 {bt["eany"]}/{bt["n"]}</div></div>')
    H.append(f'<div class="tile"><div class="tnum" style="color:var(--warn-tx)">{bt["draws"]/n*100:.0f}%</div>'
             f'<div class="mut" style="font-size:11px;margin-top:2px">实际平局率</div></div>')
    H.append('</div>')

    # ④ 概率雷达（纯模型概率，滑块按概率筛选）
    H.append('<div class="sec">概率雷达 · 拖动按概率筛选</div><div class="card">')
    if radar:
        H.append('<div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">'
                 '<label class="mut" style="font-size:13px;white-space:nowrap">模型概率 ≥</label>'
                 '<input type="range" min="50" max="90" value="50" step="5" id="sld" style="flex:1">'
                 '<span style="font-size:15px;font-weight:700;min-width:42px" id="out">50%</span></div>')
        H.append(f'<div class="row" style="margin-bottom:4px"><span class="dim" style="font-size:11px" id="cnt"></span>'
                 f'<span class="dim" style="font-size:11px">{batch_tag} · 各买法</span></div>')
        H.append('<div id="list"></div>')
        H.append('<div class="note">基于模型比分概率分布算出的各类买法（胜平负 / 不败 / 大小球 / 双方进球），'
                 '只列出模型概率 ≥50% 的项，按概率降序。这是模型「信心排序」，概率高不等于有投注价值。</div></div>')
        radar_json = json.dumps(radar, ensure_ascii=False)
        H.append('<script>var R=%s;\n%s</script>' % (radar_json, RADAR_JS))
    else:
        H.append('<div class="mut">当前没有即将开赛的场次，暂无概率买法。</div></div>')

    H.append('<div class="foot">数据：openfootball + World Football Elo Ratings。'
             '本页脚本自动生成，比赛结束后逐场更新。</div></div>')
    H.append('</body></html>')
    return "\n".join(H)


def main():
    rows = json.load(open('fixtures_online_latest.json', encoding='utf-8'))
    def predict_fn(r):
        return build_prediction(r)  # 预测方向=模型胜平负
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
    json.dump({"played": played_keys, "updated_at": bj_now().isoformat()},
              open(os.path.join(BASE, "_published_state.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[done] 已生成 {out} ({len(htmls)} 字节)，新结算 {upd} 场，已完赛 {len(played_keys)} 场")
    return out


if __name__ == "__main__":
    main()
