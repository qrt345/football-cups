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
"""
import subprocess, os, sys, json, shutil, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

CONTENT_FILES = ["predictions_log.json", "fixtures_online_latest.json", "odds_snapshot.json"]


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

    import generate_dashboard as gen
    gen.main()

    now = load_played()
    new_played = now - prev
    played_changed = (now != prev)

    stamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

    # 1) 内容文件的真实变化
    to_add = [f for f in CONTENT_FILES if os.path.exists(f) and git_dirty(f)]
    content_changed = bool(to_add)

    # 2) 是否需要刷新页面 = 有任何实质变化 / 完赛集合变化 / 首次发布
    refresh_page = content_changed or played_changed or (not prev)

    if refresh_page:
        src = os.path.join(BASE, "dist", "index.html")
        if os.path.exists(src):
            shutil.copy(src, os.path.join(BASE, "index.html"))
            to_add.append("index.html")
        # _published_state 只随页面一起提交，避免 updated_at 时间戳单独刷屏
        if os.path.exists(os.path.join(BASE, "_published_state.json")):
            to_add.append("_published_state.json")

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
