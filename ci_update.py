#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探测：GitHub Actions runner 能否访问韦德 BetVictor。跑完会被还原成正常 ci_update.py。"""
import urllib.request, ssl, json, socket

ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read(8000).decode("utf-8", "replace")
            return {"status": r.status, "final_url": r.geturl(),
                    "server": r.headers.get("Server"), "cf_ray": r.headers.get("CF-RAY"),
                    "cf_mitigated": r.headers.get("cf-mitigated"),
                    "set_cookie": (r.headers.get("Set-Cookie") or "")[:80],
                    "snippet": body[:400].replace("\n", " ")}
    except urllib.error.HTTPError as e:
        body = e.read(4000).decode("utf-8", "replace")
        return {"status": e.code, "final_url": url, "server": e.headers.get("Server"),
                "cf_ray": e.headers.get("CF-RAY"), "cf_mitigated": e.headers.get("cf-mitigated"),
                "snippet": body[:400].replace("\n", " ")}
    except Exception as ex:
        return {"error": type(ex).__name__ + ": " + str(ex)[:200]}

def main():
    print("=" * 60)
    print("PROBE: BetVictor 云端可达性探测")
    print("=" * 60)

    # 1) runner 公网 IP + 地理位置
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=20, context=ctx) as r:
            info = json.loads(r.read().decode())
        print(f"[runner IP] {info.get('ip')}  地区={info.get('country')}/{info.get('region')}/{info.get('city')}  org={info.get('org')}")
    except Exception as e:
        print(f"[runner IP] 查询失败: {e}")

    # 2) 各域名探测
    targets = [
        "https://www.betvictor281.com/zh-cn",
        "https://www.betvictor281.com/zh-cn/search",
        "https://www.betvictor.com/",
        "https://www.betvictor281.com/",
    ]
    for url in targets:
        try:
            host = url.split("/")[2]
            ip = socket.gethostbyname(host)
        except Exception as e:
            ip = f"DNS失败({e})"
        print("\n" + "-" * 50)
        print(f"URL: {url}")
        print(f"DNS: {ip}")
        res = fetch(url)
        for k, v in res.items():
            if v:
                print(f"  {k}: {v}")

    print("\n" + "=" * 60)
    print("PROBE END")

if __name__ == "__main__":
    main()
