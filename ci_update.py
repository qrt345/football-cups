#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 云端自动更新（替代本地 cron + auto_update.py）。

在 GitHub 数据中心内运行，不依赖用户本地电脑开机：
  1. generate_dashboard.main()：联网抓赛果 → 锁存预测 → 回测 → 渲染 dist/index.html
     （同时更新状态文件 fixtures_online_latest / predictions_log / _published_state）
  2. 实质变化检测（防止每30分钟空提交刷屏）：
       · 内容文件（predictions_log / fixtures_online_latest / odds_snapshot）git 有真实 diff → 视为有变化
       · played 集合变化（有新完赛）→ 有变化
       · 有变化时才把 dist/index.html 拷到根 index.html 并提交，连同状态文件一起 push
       · 啥都没变 → 不提交（index.html 里的时间戳不会进 commit）
  3. git commit & push 回 main（Pages 自动服务新页面）

认证：依赖 actions/checkout 持久化的 GITHUB_TOKEN，无需 .env.github。
本地演练：git 命令失败不崩，仅打印将提交的文件清单，方便验证 generate 链路。

赛程自动刷新（解决淘汰赛占位符 W89/2F 不更新问题）：
  每次运行检查距上次重抓赛程是否超过 REFRESH_HOURS，超过则先跑 fetch_online_fixtures
  重抓对阵+Elo+近期进失球（合并时保留已有赛果），再走正常生成。
  时间戳记录在 _last_fetch.json 并提交回仓库，让节流跨云端运行生效。
  fetch 失败时优雅降级：保留现有缓存，继续赛果更新，不中断。
"""
import subprocess, os, sys, json, shutil, datetime as dt, re

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

CONTENT_FILES = ["predictions_log.json", "fixtures_online_latest.json", "handicap_snapshot.json"]

REFRESH_HOURS = 6                       # 每隔多少小时重抓一次赛程+Elo
FETCH_STAMP = "_last_fetch.json"        # 记录上次重抓时间（提交回仓库以跨云端运行生效）
FIXTURES = "fixtures_online_latest.json"


def _has_score(r):
    return bool(r.get("score") and r["score"].get("ft"))


def _bj_date(iso_str):
    return dt.datetime.fromisoformat(iso_str).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d')

def _fkey(r):
    return f"{r['home']['source_name']}|{r['away']['source_name']}|{_bj_date(r['kickoff'])}"


def _should_refresh():
    p = os.path.join(BASE, FETCH_STAMP)
    if not os.path.exists(p):
        return True
    try:
        last = dt.datetime.fromisoformat(json.load(open(p, encoding="utf-8"))["at"])
    except Exception:
        return True
    return (dt.datetime.now(dt.timezone.utc) - last).total_seconds() >= REFRESH_HOURS * 3600


def refresh_fixtures(stamp):
    """重抓赛程+Elo，合并保留赛果。成功返回 True，失败降级返回 False。"""
    try:
        import fetch_online_fixtures as fof
        fresh = fof.build_fixtures(fof.WORLDCUP_URL, 8)  # 联网抓对阵+Elo+近期进失球
        if not fresh or len(fresh) < 32:
            print(f"[{stamp}] 赛程刷新：抓到 {len(fresh) if fresh else 0} 场，数据异常，跳过。")
            return False
        old = json.load(open(os.path.join(BASE, FIXTURES), encoding="utf-8"))
        oldmap = {_fkey(r): r for r in old}
        for r in fresh:                 # 保险：新数据缺赛果时用旧的补回
            k = _fkey(r)
            if not _has_score(r) and k in oldmap and _has_score(oldmap[k]):
                r["score"] = oldmap[k]["score"]
        json.dump(fresh, open(os.path.join(BASE, FIXTURES), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        # 记录本次重抓时间
        json.dump({"at": dt.datetime.now(dt.timezone.utc).isoformat()},
                  open(os.path.join(BASE, FETCH_STAMP), "w", encoding="utf-8"))
        ph = sum(1 for r in fresh
                 if re.match(r'^(Winner|Loser|[12][A-L]|[WL]\d)', str(r['home']['source_name']))
                 or re.match(r'^(Winner|Loser|[12][A-L]|[WL]\d)', str(r['away']['source_name'])))
        print(f"[{stamp}] 赛程刷新成功：{len(fresh)} 场，剩余占位符 {ph}（未打出的轮次，正常）。")
        return True
    except Exception as e:
        print(f"[{stamp}] 赛程刷新失败(降级，保留现有缓存): {type(e).__name__}: {e}")
        return False


def refresh_handicap(stamp):
    """抓 the-odds-api 让球盘快照。成功返回 True，失败/无 key 降级返回 False。"""
    try:
        import fetch_handicap as fh
        if not fh.API_KEY:
            print(f"[{stamp}] 让球盘：无 ODDS_API_KEY，跳过。")
            return False
        data, remaining = fh.fetch_spreads()
        odds = {}
        for ev in data:
            line = fh.pick_line(ev)
            if line:
                odds[f"{fh._norm(line['home_team'])}|{fh._norm(line['away_team'])}"] = line
        if not odds:
            print(f"[{stamp}] 让球盘：无可用数据，跳过。")
            return False
        snap = {"updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M 北京时间"),
                "source": "the-odds-api spreads", "count": len(odds), "handicap": odds}
        json.dump(snap, open(os.path.join(BASE, "handicap_snapshot.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"[{stamp}] 让球盘刷新成功：{len(odds)} 场，API 剩余 {remaining}。")
        return True
    except Exception as e:
        print(f"[{stamp}] 让球盘刷新失败(降级): {type(e).__name__}: {e}")
        return False


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True)
    except Exception as ex:
        class R:  # 本地无 git 时的兜底
            returncode = -1; stdout = ""; stderr = str(ex)
        return R()


def git_dirty(path):
    r = sh("git", "status", "--porcelain", "--", path)
    return bool(r.stdout.strip())


def load_played():
    sp = os.path.join(BASE, "_published_state.json")
    if os.path.exists(sp):
        try:
            return set(json.load(open(sp, encoding="utf-8")).get("played", []))
        except Exception:
            pass
    return set()


def main():
    prev = load_played()

    stamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

    # 0) 低频重抓赛程+Elo（解决淘汰赛占位符不更新）+ 让球盘；失败降级不影响赛果流程
    fetched = False
    if _should_refresh():
        fetched = refresh_fixtures(stamp)
        refresh_handicap(stamp)   # 让球盘随赛程一起刷新（同 6h 节流）

    import generate_dashboard as gen
    gen.main()

    now = load_played()
    new_played = now - prev
    played_changed = (now != prev)

    # 1) 内容文件的真实变化
    to_add = [f for f in CONTENT_FILES if os.path.exists(f) and git_dirty(f)]
    content_changed = bool(to_add)

    # 2) 是否需要刷新页面 = 有任何实质变化 / 完赛集合变化 / 首次发布 / 刚重抓过赛程
    refresh_page = content_changed or played_changed or fetched or (not prev)

    if refresh_page:
        src = os.path.join(BASE, "dist", "index.html")
        if os.path.exists(src):
            shutil.copy(src, os.path.join(BASE, "index.html"))
            to_add.append("index.html")
        # _published_state 只随页面一起提交，避免 updated_at 时间戳单独刷屏
        if os.path.exists(os.path.join(BASE, "_published_state.json")):
            to_add.append("_published_state.json")

    # _last_fetch.json：刚重抓过就提交，让 6h 节流跨云端运行生效
    if fetched and os.path.exists(os.path.join(BASE, FETCH_STAMP)):
        to_add.append(FETCH_STAMP)

    # 去重 + 仅保留确实存在且 git 认为有改动的
    seen, final = set(), []
    for f in to_add:
        if f in seen or not os.path.exists(f):
            continue
        seen.add(f)
        if git_dirty(f):
            final.append(f)

    if not final:
        print(f"[{stamp}] 无实质变化，跳过提交。已完赛 {len(now)} 场。")
        return 0

    sh("git", "config", "user.name", "github-actions[bot]")
    sh("git", "config", "user.email", "github-actions[bot]@users.noreply.github.com")
    sh("git", "add", "--", *final)
    if new_played:
        msg = f"auto: {stamp} (+{len(new_played)} 新完赛, 共 {len(now)} 场)"
    elif "index.html" in final:
        msg = f"auto: {stamp} 页面/预测刷新 (共 {len(now)} 场)"
    else:
        msg = f"auto: {stamp} 状态更新"
    c = sh("git", "commit", "-m", msg)
    if c.returncode != 0:
        print(f"[{stamp}] commit 跳过/失败: {c.stdout} {c.stderr}".strip())
        return 0
    p = sh("git", "push", "origin", "HEAD:main")
    if p.returncode == 0:
        print(f"[{stamp}] 已提交并推送: {final}")
    else:
        print(f"[{stamp}] commit 完成但 push 失败(本地演练正常): {p.stderr[-300:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
