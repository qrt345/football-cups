# 世界杯预测看板项目 — 交接文档 (HANDOFF)

> 新窗口打开后，先读这个文件即可快速接手。开口说"读 G:\桌面\Claudecode\世界杯\HANDOFF.md 接着做世界杯预测看板"。

---

## 0. 一句话

全自动的世界杯比分预测 + 价值分析**看板网页**，固定网址、内容随比赛进程自动更新（每30分钟检查，比赛结束逐场更新、当天全完停更）。

**🔗 线上地址（永久不变）：https://qrt345.github.io/football-cups/**

---

## 1. 项目位置 & 终端注意

```
G:\桌面\Claudecode\世界杯
```
- 终端用 **git-bash**，路径含中文。cd 时用单引号：`cd '/g/桌面/Claudecode/世界杯' && ...`
- **不要用 terminal 的 workdir 参数**（含"桌"字会被拦截），改成命令里 `cd '...' &&`
- 系统时间 = 北京时间 UTC+8（cron 的 naive 时间戳按北京时间解释）

---

## 2. 核心文件（按重要性）

| 文件 | 作用 |
|---|---|
| `generate_dashboard.py` | **看板生成器（核心）**：刷新赛果→锁存预测→回测→生成 dist/index.html |
| `predictions_store.py` | **预测锁存**：开赛前冻结预测到 predictions_log.json，赛后只读不重算 |
| `auto_update.py` | **变化检测+部署**：有新完赛才重新生成+上传GitHub，否则静默跳过 |
| `score_predictor.py` | 预测模型：Elo+攻防+近期进失球→xG→泊松-负二项混合模型 |
| `odds_snapshot.json` | 韦德(BetVictor)让分/大小/赔率**快照**（赛前手动/低频更新，含时间戳） |
| `predictions_log.json` | **冻结的预测日志（真相源）**，自动备份到仓库 data/ |
| `fixtures_online_latest.json` | 赛程+Elo+攻防+近期进失球缓存（含已完赛 score 字段） |
| `dist/index.html` | 生成的看板页面（部署到 GitHub Pages） |
| `.env.github` | GitHub 凭据（user/repo/token，已 gitignore，勿外传） |
| `.env.feishu` | 飞书 webhook（已 gitignore） |
| `fetch_online_fixtures.py` | 联网抓赛程+Elo，生成 fixtures JSON（备用） |
| `value_full.py` | 早期 EV 分析脚本（以模型概率为基准算盘口EV） |

`~/.hermes/scripts/wc_auto_update.py` — cron 启动器（no_agent 脚本必须放这里），调用项目里的 auto_update.py

---

## 3. 看板四大板块

1. **历史预测准确度**：预测方向命中 / Top1精确比分 / Top2任一精确 / 实际平局率（KPI比例条）+ 锁存场单独统计
2. **最新比分·预测vs实际**：实际比分 / 预测首选 / **预测方向** / 命中标签 / 赛前锁存·回溯标记
3. **接下来预测**：xG、胜平负、**首选比分 + 次选比分**（防冷/次概率自动二选一）
4. **我的方向×韦德盘口EV**：各买法的模型概率、实盘赔率、EV；侧栏置顶"方向∩正EV核心"

### 关键定义
- **预测方向**：赛前看韦德**亚洲让分盘**，模型在该盘口选概率高的一边（如"英格兰 -2"），赛后判盘赢/走水/盘输；**拿不到让分盘时退回胜平负**（命中/未中）
- **次选比分**：接近局/平局概率高(优势方<60%或平≥22%)→**防冷**(最高概率平局比分)；否则→**次概率**(第二高概率比分)
- **赛前锁存** = 开赛前冻结的真实预测；**回溯** = 历史场按同模型补录（不计入锁存命中率）

---

## 4. 模型 & 方法论

**预测模型**：xG = base × 中性主场 × 攻防 × Elo强度 × 近期进失球；混合 65%Poisson + 35%NegBinom(dispersion=7.5)。
**优势方修正**（match-only）：Elo差≥400或xG比≥3.5→强×0.97/弱×1.04；≥180或≥2.0→×0.93/×1.06；否则×0.96/×1.04（重视平局）。

**投注价值方法论（用户核心偏好，重要）**：
- **以模型概率为基准**，套盘口赔率算 `EV = 模型概率 × 赔率 − 1`（**不要用赔率反推预测**）
- 每次输出三桶结构：①方向∩正EV交集表(标绝对核心) ②正EV但反方向(仅标注) ③方向对但被压价(负EV)
- 波胆抽水重基本负EV，仅娱乐
- **不主动给凯利仓位/不提现金余额**，用户要分仓才给；真钱下注用户手动确认

---

## 5. 自动更新链路

```
GitHub cron「世界杯看板自动更新」每30分钟
  → ~/.hermes/scripts/wc_auto_update.py
  → auto_update.py：抓最新赛果 → 对比 _published_state.json 已发布完赛集合
     · 有新比赛结束 → generate_dashboard 重新生成 → GitHub API 上传 index.html + 备份 predictions_log
     · 无新结果 → 静默跳过(wakeAgent:false)
     · 当天全完→自然停更；次日有新完赛→自动恢复
```

**cron 管理（CLI trampoline 坏了，用 Python）**：
```python
import sys, os
sys.path.insert(0, r"C:\Users\Administrator\.hermes-web-ui\desktop-runtime\hermes\0.16.0\win-x64\python\Lib\site-packages")
os.environ.setdefault("HERMES_HOME", r"C:\Users\Administrator\.hermes")
from tools.cronjob_tools import cronjob
print(cronjob(action="list"))   # 查看任务
```
cron 任务名「世界杯看板自动更新」，schedule `every 30m`，no_agent=True，script `wc_auto_update.py`。
jobs.json 在 `C:\Users\Administrator\.hermes\cron\jobs.json`。

---

## 6. 部署机制（GitHub Pages）

- 仓库：`qrt345/football-cups`，分支 main，Pages 根目录
- **git push 直连被网络墙** → 改用 **GitHub REST API 上传**（PUT /repos/{user}/{repo}/contents/{path}，base64内容+sha）
- 凭据从 `.env.github` 读取（GITHUB_USER=qrt345, GITHUB_REPO=football-cups, GITHUB_TOKEN=ghp_...）
- token 在代码里写字面量会被密钥扫描器破坏 → **始终从 .env.github 读取，别硬编码**
- Pages CDN 有 1-3 分钟构建延迟，部署后用 `?v=时间戳` + no-cache 头验证

### 手动部署/重新生成
```bash
cd '/g/桌面/Claudecode/世界杯'
python generate_dashboard.py        # 只生成本地 dist/index.html
python auto_update.py               # 生成+检测+部署(有新结果才传)
```
强制部署（不管有无新结果）：直接调 GitHub API 上传 dist/index.html（见下方代码模板）。

---

## 7. 数据源

- 赛果：`https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json`
  - **常超时**！多镜像+重试：raw.githubusercontent / cdn.jsdelivr.net / fastly.jsdelivr.net / gcore.jsdelivr.net（路径 `gh/openfootball/worldcup.json@master/2026/worldcup.json`），urllib 每个URL重试3次，timeout 25s
  - worldcup.json 的 matches[] : team1/team2 是字符串 + date + score.ft；fixtures缓存按 home.source_name/away.source_name + kickoff[:10] 匹配，也试 date-1 吸收时区差
- Elo评分：`https://www.eloratings.net/`
- 韦德赔率：BetVictor `https://www.betvictor281.com/zh-cn`（账号见下），有 **429 限速** → 用快照而非实时

---

## 8. 韦德 BetVictor 账户（真钱，余额约¥300）

```
站点: https://www.betvictor281.com/zh-cn
账号: 1622110661@qq.com
密码: lpoLPO123!
```
- 登录：体育→点登录→输邮箱→继续(先点同意cookie)→输密码→登录（会话易掉线）
- 比赛主页面有亚洲让分/大小/总进球；进具体 event 页(/sports/240/meetings/.../events/...)可读1X2(赛果投注,港赔)、波胆等
- **深层 event 页 429 频繁** → 抓不到就用现有 odds_snapshot.json，并标注赔率时间
- 港赔转欧赔：欧赔 = 港赔 + 1

### 更新盘口快照
让我抓最新韦德盘口时：登录→读四场让分/大小/主胜赔率→更新 `odds_snapshot.json` 的 odds 字典（键=`主队source_name|客队source_name`）+ updated_at 时间戳→重新生成部署。

---

## 9. GitHub API 上传代码模板

```python
import urllib.request, json, ssl, base64
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
env={}
for line in open(r"G:\桌面\Claudecode\世界杯\.env.github",encoding="utf-8"):
    if "=" in line: k,v=line.strip().split("=",1); env[k]=v
U=env["GITHUB_USER"]; REPO=env["GITHUB_REPO"]; TOKEN=env["GITHUB_TOKEN"]
def gh(path, method="GET", data=None):
    req=urllib.request.Request("https://api.github.com"+path,
        data=json.dumps(data).encode() if data is not None else None, method=method,
        headers={"Authorization":f"token {TOKEN}","Accept":"application/vnd.github+json","User-Agent":"hermes","Content-Type":"application/json"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
                t=r.read().decode(); return r.status,(json.loads(t) if t else {})
        except urllib.error.HTTPError as e:
            t=e.read().decode(); return e.code,(json.loads(t) if t else {})
        except Exception as ex:
            if i==2: return -1,{"error":str(ex)}
content=base64.b64encode(open(r"G:\桌面\Claudecode\世界杯\dist\index.html","rb").read()).decode()
st,info=gh(f"/repos/{U}/{REPO}/contents/index.html")
data={"message":"manual deploy","content":content,"branch":"main"}
if st==200 and info.get("sha"): data["sha"]=info["sha"]
print(gh(f"/repos/{U}/{REPO}/contents/index.html","PUT",data)[0])
```

---

## 10. 历史预测准确率（截至交接，全程44场回测）

- 预测方向命中、Top1精确约16%、Top2任一约32%、实际平局率约30%（这届平局偏多）
- 6/23批次：阿根廷2-0✅精确、法国3-0✅精确、挪威3-2✓方向、约旦1-2阿尔✓方向
- 核心经验：方向准、波胆难；强弱差距大敢给大比分；中等优势必防平；弱队+0.5/+1常比强队让球有价值

---

## 11. 已知坑 / 注意

1. **自动更新依赖这台电脑开机联网**（cron跑本地），关机暂停，开机自动恢复
2. token 不要硬编码进代码（密钥扫描器会破坏字面量），从 .env.github 读
3. cron deliver=origin 在CLI不回界面 → 关键结果另推飞书/微信
4. 改了 generate_dashboard.py 后，若不想手动部署，等下次 cron 同步自然生效
5. predictions_log.json 是锁存真相源，删了会丢历史预测（仓库 data/ 有备份可恢复）
6. 当前有一处改动（板块①去重「方向命中率」）已在代码里，等下次比赛同步自动上线

---

## 12. 快速接手步骤

1. `cd '/g/桌面/Claudecode/世界杯'`
2. 看 cron 状态：用第5节 Python 代码 `cronjob(action="list")`
3. 手动刷新看板：`python auto_update.py`（有新结果会自动部署）
4. 更新韦德盘口：登录抓盘→改 odds_snapshot.json→`python generate_dashboard.py`→API上传
5. 验证线上：打开 https://qrt345.github.io/football-cups/ （CDN延迟1-3分钟）
