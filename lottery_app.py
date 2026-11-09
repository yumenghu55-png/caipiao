#!/usr/bin/env python3
"""
彩票数据更新与手机网页生成器(只需 Python 3.9+,无需安装第三方库)

  python lottery_app.py            下载/更新六种彩票,并生成 site 文件夹
  python lottery_app.py --years 8  首次下载时取最近 8 年
  python lottery_app.py --demo     用模拟数据生成 site_demo,用于预览界面

开奖数据保存在 data/*.csv;打开 site/index.html 即可查看。
页面上的号码是随机生成后按形态过滤的,不能提高中奖概率。
"""
import argparse
import base64
import csv
import json
import math
import random
import re
import struct
import urllib.error
import urllib.request
import zlib
from collections import Counter
from datetime import date, datetime, timedelta
from math import comb
from pathlib import Path

GAMES = {
    "primitiva": dict(name="La Primitiva", region="西班牙", k=6, n=49, ek=0, en=0, extra="", source="sheet", id="2PACX-1vTov1BuA0nkVGTS48arpPFkc9cG7B40Xi3BfY6iqcWTrMwCBg5b50-WwvnvaR6mxvFHbDBtYFKg5IsJ/pub?gid=1"),
    "bonoloto": dict(name="Bonoloto", region="西班牙", k=6, n=49, ek=0, en=0, extra="", source="sheet", id="2PACX-1vQALTRaLDFfhXOAQmeONPqmFKm9yOiQ4W97rhWgR41BZ7czFsjK5YktD6fnETKHGB9YUnyQ4XBSbhZx/pub?gid=0"),
    "euromillones": dict(name="Euromillones", region="西班牙", k=5, n=50, ek=2, en=12, extra="星号", source="openeuro", id="EMIL"),
    "ssq": dict(name="双色球", region="福彩", k=6, n=33, ek=1, en=16, extra="蓝球", source="repo", id="ssq", strategy=True),
    "dlt": dict(name="大乐透", region="体彩", k=5, n=35, ek=2, en=12, extra="后区", source="repo", id="dlt", strategy=True),
}
UA = "Mozilla/5.0 (personal lottery statistics script)"
TICKETS_PER_GAME = 3
POOL_SIZE = 500
D3_COST, D3_SINGLE_PRIZE, D3_GROUP6_PRIZE = 2, 1040, 173  # 基本奖金,不含各地临时派奖
D3_TUO = 5  # 默认 1 胆 5 拖


# ---------- 下载 ----------
def valid(game, main, extra):
    return (
        len(main) == game["k"] and len(set(main)) == game["k"]
        and all(1 <= x <= game["n"] for x in main)
        and len(extra) == game["ek"] and len(set(extra)) == game["ek"]
        and all(1 <= x <= game["en"] for x in extra)
    )


def fetch_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8-sig")


def parse_date(text):
    """读取 dd/mm/yyyy 或 yyyy-mm-dd,返回日期和日期之后的文字。"""
    found = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if found:
        year, month, day = map(int, found.groups())
    else:
        found = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text)
        if not found:
            return None, ""
        day, month, year = map(int, found.groups())
        year += 2000 if year < 100 else 0
    try:
        return date(year, month, day), text[found.end():]
    except ValueError:
        return None, ""


def parse_sheet(game, text, start):
    draws = {}
    for line in text.splitlines():
        day, rest = parse_date(line)
        if day is None or day < start:
            continue
        main = sorted(int(x) for x in re.findall(r"\d+", rest)[:game["k"]])
        if valid(game, main, []):
            draws[day.isoformat()] = (main, [])
    return draws


def fetch_sheet(game, start):
    """Lotoideas 公开发布的西班牙历史开奖表(免费下载,更新频率不固定)。"""
    base = "https://docs.google.com/spreadsheets/d/e/"
    draws = parse_sheet(game, fetch_text(f"{base}{game['id']}&single=true&output=csv"), start)
    if not draws:
        raise ValueError("表格里没有读到有效开奖记录")
    return draws


def fetch_repo(game, start):
    """GitHub 上每天自动更新的双色球/大乐透数据。"""
    url = f"https://raw.githubusercontent.com/yangxb919/lottery-data/main/data/{game['id']}.json"
    draws = {}
    for row in json.loads(fetch_text(url)):
        low = "red" if game["id"] == "ssq" else "front"
        high = "blue" if game["id"] == "ssq" else "back"
        main = sorted(int(x) for x in row[low])
        extra = sorted(int(x) for x in row[high])
        if row["date"] >= start.isoformat() and valid(game, main, extra):
            draws[row["date"]] = (main, extra)
    if not draws:
        raise ValueError("数据为空")
    return draws


def fetch_open_euro(game, start):
    """公开的 Euromillones CSV 存档,自动更新,仅供个人非商业使用。"""
    url = "https://raw.githubusercontent.com/daowa89/lottery-archive/main/eu/euromillions/results.csv"
    lines = fetch_text(url).splitlines()
    draws = {}
    for row in csv.DictReader(lines):
        main = sorted(int(row[f"n{i}"]) for i in range(1, 6))
        extra = sorted(int(row[f"s{i}"]) for i in range(1, 3))
        if row["date"] >= start.isoformat() and valid(game, main, extra):
            draws[row["date"]] = (main, extra)
    if not draws:
        raise ValueError("数据为空")
    return draws


# 全部读取公开整理的数据,不再请求拒绝脚本访问的官网
FETCHERS = {"openeuro": fetch_open_euro, "sheet": fetch_sheet, "repo": fetch_repo}
D3_AUTO = True


# ---------- 本地数据 ----------
def load(path, game):
    draws = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                main = sorted(int(row[f"n{i + 1}"]) for i in range(game["k"]))
                extra = sorted(int(row[f"e{i + 1}"]) for i in range(game["ek"]))
                if valid(game, main, extra):
                    draws[row["date"]] = (main, extra)
    return draws


def save(path, game, draws):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date"] + [f"n{i + 1}" for i in range(game["k"])] + [f"e{i + 1}" for i in range(game["ek"])])
        for day in sorted(draws):
            writer.writerow([day] + draws[day][0] + draws[day][1])


def demo_draws(game, seed):
    rng = random.Random(seed)
    today = date.today()
    return {
        (today - timedelta(days=3 * i)).isoformat(): (
            sorted(rng.sample(range(1, game["n"] + 1), game["k"])),
            sorted(rng.sample(range(1, game["en"] + 1), game["ek"])) if game["ek"] else [],
        )
        for i in range(600)
    }


# ---------- 号码生成 ----------
def sum_range(game):
    """用数学上的随机抽样得到和值中间 80% 的范围,不依赖历史“冷热”。"""
    rng = random.Random(f"range-{game['k']}-{game['n']}")
    sums = sorted(sum(rng.sample(range(1, game["n"] + 1), game["k"])) for _ in range(20000))
    return sums[2000], sums[18000]


def longest_run(numbers):
    best = run = 1
    for a, b in zip(numbers, numbers[1:]):
        run = run + 1 if b - a == 1 else 1
        best = max(best, run)
    return best


def passes(game, main, bounds):
    """形态过滤:只影响号码外观及是否和大众选号相近,不改变中奖概率。"""
    odd = sum(x % 2 for x in main)
    zones = {0 if x < 10 else x // 10 for x in main}
    if odd in (0, game["k"]) or not bounds[0] <= sum(main) <= bounds[1]:
        return False
    if len(zones) < 3 or longest_run(main) >= 3:
        return False
    # 号码范围较大时,避开全部落在 1-31 的“生日号码”
    return not (game["n"] >= 45 and main[-1] <= 31)


def draw_main(game, rng):
    return sorted(rng.sample(range(1, game["n"] + 1), game["k"]))


def draw_filtered(game, rng, bounds):
    while True:
        main = draw_main(game, rng)
        if passes(game, main, bounds):
            return main


def make_tickets(game, seed, bounds):
    """当天结果固定;几注之间尽量少重复,方便朋友各选一注。"""
    rng = random.Random(seed)
    tickets, used = [], set()
    for _ in range(TICKETS_PER_GAME):
        best = None
        for _ in range(3000):
            main = draw_filtered(game, rng, bounds)
            overlap = len(used & set(main))
            if best is None or overlap < best[0]:
                best = (overlap, main)
            if overlap == 0:
                break
        used |= set(best[1])
        extra = []
        if game["ek"]:
            for _ in range(100):  # 让几注的特别号尽量不完全相同
                extra = sorted(rng.sample(range(1, game["en"] + 1), game["ek"]))
                if all(extra != ticket["extra"] for ticket in tickets):
                    break
        tickets.append({"main": best[1], "extra": extra})
    return tickets


def backtest(game, rows, bounds):
    """比较“过滤选号”和“完全随机选号”与历史开奖的命中情况。"""
    rng = random.Random(f"backtest-{game['name']}")
    pools = {
        "filtered": [set(draw_filtered(game, rng, bounds)) for _ in range(POOL_SIZE)],
        "random": [set(draw_main(game, rng)) for _ in range(POOL_SIZE)],
    }
    winners = [set(main) for main, _ in rows[-600:]]
    result = {}
    for name, pool in pools.items():
        hits = [len(ticket & winner) for ticket in pool for winner in winners]
        result[name] = {
            "average": round(sum(hits) / len(hits), 3),
            "threePlus": round(100 * sum(h >= 3 for h in hits) / len(hits), 2),
        }
    return result


# ---------- 双色球/大乐透的特殊策略 ----------
def rank_numbers(counts, size):
    return [i for _, i in sorted((-counts[i], i) for i in range(1, size + 1))]


def mix_ticket(game, rng, counts, bounds):
    """冷热搭配:从出现最多的三分之一和最少的三分之一里各取一部分。"""
    ranked = rank_numbers(counts, game["n"])
    third = game["n"] // 3
    hot, cold = ranked[:third], ranked[-third:]
    hot_count = (game["k"] + 1) // 2
    main = []
    for _ in range(2000):
        main = sorted(rng.sample(hot, hot_count) + rng.sample(cold, game["k"] - hot_count))
        if passes(game, main, bounds):
            break
    return main


def popularity(x):
    """彩民偏好的粗略估计,没有逐号销量数据。分数越低,估计选的人越少。"""
    score = 0
    if x <= 12:  # 月份
        score += 1
    if x <= 31:  # 日期
        score += 1
    if x % 10 in (6, 8, 9):  # 吉利尾数
        score += 1
    if "4" in str(x):  # 不少人回避
        score -= 1
    return score


def unlikely_ticket(game, rng, counts):
    """“最不可能”的组合:偏冷、估计少有人选,并包含一组看起来不像随机的连号。"""
    order = sorted(range(1, game["n"] + 1), key=lambda x: (popularity(x), counts[x], rng.random()))
    candidates = order[:max(game["k"] * 2, game["n"] // 2)]
    main = []
    for _ in range(2000):
        main = sorted(rng.sample(candidates, game["k"]))
        if longest_run(main) == 2:
            break
    return main


def strategy_extra(game, rng, extra_counts, kind):
    ranked = rank_numbers(extra_counts, game["en"])
    third = max(game["ek"], game["en"] // 3)
    hot, cold = ranked[:third], ranked[-third:]
    if kind == "unlikely":
        return sorted(rng.sample(cold, game["ek"]))
    if game["ek"] == 1:
        return [rng.choice(hot + cold)]
    first = rng.choice(hot)
    return sorted([first, rng.choice([x for x in cold if x != first])])


def strategy_backtest(game, rows, bounds):
    """逐期回测:每一期只用它之前的历史数据选号,避免用到未来信息。"""
    rng = random.Random(f"strategy-{game['name']}")
    start = max(len(rows) // 2, len(rows) - 300)
    counts = Counter(x for main, _ in rows[:start] for x in main)
    hits = {"mix": [], "unlikely": [], "random": []}
    for main, _ in rows[start:]:
        winner = set(main)
        for _ in range(20):
            hits["mix"].append(len(winner & set(mix_ticket(game, rng, counts, bounds))))
            hits["unlikely"].append(len(winner & set(unlikely_ticket(game, rng, counts))))
            hits["random"].append(len(winner & set(draw_main(game, rng))))
        counts.update(main)
    return {
        name: {
            "average": round(sum(v) / len(v), 3),
            "threePlus": round(100 * sum(h >= 3 for h in v) / len(v), 2),
        }
        for name, v in hits.items()
    }


def strategies(game, rows, bounds, today):
    rng = random.Random(f"{today}-{game['name']}-strategy")
    counts = Counter(x for main, _ in rows for x in main)
    extra_counts = Counter(x for _, extra in rows for x in extra)
    return {
        "mix": {"main": mix_ticket(game, rng, counts, bounds),
                "extra": strategy_extra(game, rng, extra_counts, "mix")},
        "unlikely": {"main": unlikely_ticket(game, rng, counts),
                     "extra": strategy_extra(game, rng, extra_counts, "unlikely")},
        "backtest": strategy_backtest(game, rows, bounds),
    }


# ---------- 统计 ----------
def chi_square_p(stat, df):
    z = ((stat / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return 0.5 * math.erfc(z / math.sqrt(2))


def summarize(game, draws, today):
    days = sorted(draws)
    rows = [draws[d] for d in days]
    total, k, n = len(rows), game["k"], game["n"]
    frequency = Counter(x for main, _ in rows for x in main)
    expected = total * k / n
    stat = sum((frequency[i] - expected) ** 2 / expected for i in range(1, n + 1)) / (1 - k / n)
    ranked = sorted(((frequency[i], i) for i in range(1, n + 1)), key=lambda p: (-p[0], p[1]))
    odd = Counter(sum(x % 2 for x in main) for main, _ in rows).most_common(1)[0]
    sums = sorted(sum(main) for main, _ in rows)
    bounds = sum_range(game)
    return {
        "name": game["name"], "region": game["region"], "extraLabel": game["extra"],
        "total": total, "first": days[0], "lastDate": days[-1],
        "lastMain": rows[-1][0], "lastExtra": rows[-1][1],
        "tickets": make_tickets(game, f"{today}-{game['name']}", bounds),
        "hot": [{"n": i, "c": c} for c, i in ranked[:5]],
        "cold": [{"n": i, "c": c} for c, i in ranked[:-6:-1]],
        "expected": round(expected, 1), "p": round(chi_square_p(stat, n - 1), 3),
        "oddMost": {"odd": odd[0], "share": round(100 * odd[1] / total, 1)},
        "sumRange": [sums[int(total * .1)], sums[int(total * .9)]],
        "backtest": backtest(game, rows, bounds),
        "strategies": strategies(game, rows, bounds, today) if game.get("strategy") else None,
    }


# ---------- 福彩3D ----------
def fetch_d3(start):
    """GitHub 公开镜像,项目说明允许外部程序直接读取;历史较短,本地会逐日累积。"""
    base = "https://raw.githubusercontent.com/wenjinliuu/lottery-data-repo/main/public_data/v2/by-year/fc3d"
    draws = {}
    for year in range(start.year, date.today().year + 1):
        try:
            payload = json.loads(fetch_text(f"{base}/{year}.json"))
        except urllib.error.HTTPError as error:
            if error.code == 404:  # 这一年镜像里没有
                continue
            raise
        for row in payload["draws"]:
            digits = [int(x) for x in row["numbers"]["digits"]]
            if len(digits) == 3 and all(0 <= x <= 9 for x in digits) and row["date"] >= start.isoformat():
                draws[row["date"]] = digits
    if not draws:
        raise ValueError("数据为空")
    return draws


def load_d3(path):
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        return {r["date"]: [int(r["d1"]), int(r["d2"]), int(r["d3"])] for r in csv.DictReader(f)}


def save_d3(path, draws):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "d1", "d2", "d3"])
        for day in sorted(draws):
            writer.writerow([day] + draws[day])


def d3_plan(rng):
    """胆码和拖码完全随机:3D 每位数字等概率,历史冷热没有预测作用。"""
    digits = rng.sample(range(10), 1 + D3_TUO)
    return digits[0], sorted(digits[1:])


def d3_hit(dan, tuo, draw):
    return len(set(draw)) == 3 and dan in draw and all(x == dan or x in tuo for x in draw)


def summarize_d3(draws, today):
    days = sorted(draws)
    rows = [draws[d] for d in days]
    total = len(rows)
    rng = random.Random(f"{today}-d3")
    dan, tuo = d3_plan(rng)
    expanded = [sorted([dan, a, b]) for i, a in enumerate(tuo) for b in tuo[i + 1:]]
    plans = []
    for size in (3, 5, 7):
        notes = comb(size, 2)
        plans.append({
            "tuo": size, "notes": notes, "cost": notes * D3_COST,
            "prob": round(notes * 0.6, 1), "ret": round(notes * 6 / 1000 * D3_GROUP6_PRIZE, 2),
        })
    tester = random.Random("d3-backtest")
    pool = [d3_plan(tester) for _ in range(POOL_SIZE)]
    recent = rows[-600:]
    hits = sum(d3_hit(d, t, draw) for d, t in pool for draw in recent)
    frequency = Counter(x for row in rows for x in row)
    ranked = sorted(((frequency[i], i) for i in range(10)), key=lambda p: (-p[0], p[1]))
    shape = Counter(len(set(row)) for row in rows)
    single = []
    while len(single) < TICKETS_PER_GAME:
        pick = [rng.randrange(10) for _ in range(3)]
        if pick not in single:
            single.append(pick)
    return {
        "total": total, "first": days[0], "lastDate": days[-1], "last": rows[-1],
        "dan": dan, "tuo": tuo, "expanded": expanded, "single": single, "plans": plans,
        "singlePrize": D3_SINGLE_PRIZE, "groupPrize": D3_GROUP6_PRIZE, "cost": D3_COST,
        "hitRate": round(100 * hits / (POOL_SIZE * len(recent)), 2),
        "shares": {k: round(100 * shape[v] / total, 1) for k, v in (("six", 3), ("three", 2), ("triple", 1))},
        "hot": [{"n": i, "c": c} for c, i in ranked[:3]],
        "cold": [{"n": i, "c": c} for c, i in ranked[:-4:-1]],
    }


# ---------- 网页 ----------
def make_icon(size):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    raw = bytearray()
    middle = size / 2
    for y in range(size):
        raw.append(0)
        for x in range(size):
            inside = (x - middle) ** 2 + (y - middle) ** 2 <= (size * .3) ** 2
            raw += bytes((255, 255, 255) if inside else (29, 78, 137))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>每日号码</title>
<link rel="manifest" href="manifest.json">
<link rel="apple-touch-icon" href="icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="每日号码">
<style>
:root{--bg:#f4f6f9;--card:#fff;--ink:#17202c;--soft:#5f6b7a;--line:#e0e5ec;--main:#1d4e89;--extra:#c2410c;--warn:#fff4e5;--warnink:#7a4100}
@media (prefers-color-scheme:dark){:root{--bg:#10151c;--card:#1a212b;--ink:#eef2f7;--soft:#a2adbb;--line:#2d3744;--main:#5b9bd8;--extra:#f08a4b;--warn:#3a2a14;--warnink:#ffd7a0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
header{padding:20px 16px 8px}h1{font-size:26px;margin:0}.sub{color:var(--soft);font-size:13px}
nav{display:flex;gap:8px;overflow-x:auto;padding:8px 16px;position:sticky;top:0;background:var(--bg);z-index:2}
nav button{flex:none;min-height:40px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:20px;padding:0 14px;font:inherit}
nav button.on{background:var(--main);border-color:var(--main);color:#fff}
main{padding:8px 16px 28px;max-width:640px;margin:auto}section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:0 0 12px}
h2{font-size:16px;margin:0 0 10px}.row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.ticket{padding:10px 0;border-top:1px solid var(--line)}.ticket:first-of-type{border-top:0}.label{color:var(--soft);font-size:13px;margin-bottom:6px}
.b{width:36px;height:36px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-weight:650;font-variant-numeric:tabular-nums;background:var(--main);color:#fff}.x{background:var(--extra)}
.sep{width:6px}.note{background:var(--warn);color:var(--warnink);border-radius:10px;padding:12px;font-size:14px}.demo{background:#b42318;color:#fff;padding:8px 16px;text-align:center;font-weight:650}
dl{display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px 12px;margin:0;font-size:14px}dt{color:var(--soft);white-space:nowrap}dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
.err{color:var(--extra);font-size:14px}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:4px;margin:8px 16px;background:var(--line);padding:4px;border-radius:12px}.seg button{min-height:44px;border:0;background:transparent;color:var(--ink);border-radius:9px;font:inherit;font-weight:650}.seg button.on{background:var(--card);color:var(--main)}
.d{background:var(--extra)}.expand{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;font-size:15px;font-variant-numeric:tabular-nums;text-align:center}table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}th,td{padding:7px 4px;text-align:right;border-top:1px solid var(--line)}th{color:var(--soft);font-weight:500;border-top:0}th:first-child,td:first-child{text-align:left}.mt{margin-top:12px}
</style></head><body>
<div id="demo" class="demo" hidden>模拟数据，仅用于预览界面</div>
<header><h1>每日号码</h1><div class="sub" id="updated"></div></header>
<div class="seg" id="modes"><button data-m="dantuo">胆拖买法</button><button data-m="single">单买一注</button></div>
<nav id="tabs"></nav><main id="view"></main>
<script>
const DATA=__DATA__;
const $=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e};
const pad=n=>String(n).padStart(2,"0");
function balls(main,extra){const r=$("div","row");main.forEach(n=>r.append($("span","b",pad(n))));if(extra.length){r.append($("span","sep"));extra.forEach(n=>r.append($("span","b x",pad(n))))}return r}
function stats(items){const d=$("dl");items.forEach(([a,b])=>{d.append($("dt","",a),$("dd","",b))});return d}
function digits(arr){const r=$("div","row");arr.forEach(n=>r.append($("span","b",String(n))));return r}
function table(head,rows){const t=$("table"),h=$("tr");head.forEach(x=>h.append($("th","",x)));t.append(h);rows.forEach(r=>{const w=$("tr");r.forEach(x=>w.append($("td","",x)));t.append(w)});return t}
const note=x=>$("section","note",x);
const state={mode:"dantuo",key:DATA.order[0][0]};
try{state.mode=localStorage.getItem("mode")||state.mode;const k=localStorage.getItem("tab");if(k&&DATA.order.some(o=>o[0]===k))state.key=k}catch(e){}
function empty(v){const s=$("section");s.append($("p","err","这个彩种暂时没有数据，请看电脑上的运行提示。"));v.append(s)}
function dantuo(v){
 const g=DATA.d3;if(!g)return empty(v);
 const p=g.plans.find(x=>x.tuo===g.tuo.length);
 const a=$("section");a.append($("h2","","福彩3D · 今日胆拖（组选六）"),$("div","label","胆码"));
 const dr=$("div","row");dr.append($("span","b d",String(g.dan)));a.append(dr,$("div","label mt","拖码"),digits(g.tuo));
 a.append($("div","mt"),stats([["注数",p.notes+" 注"],["成本",p.cost+" 元"],["中奖概率",p.prob+"%"],["中奖可得",g.groupPrize+" 元（净赚 "+(g.groupPrize-p.cost)+" 元）"],["平均返还",p.ret+" 元 / "+p.cost+" 元"]]));
 const e=$("section");e.append($("h2","","展开后的 "+p.notes+" 注组选六"));const x=$("div","expand");g.expanded.forEach(n=>x.append($("span","",n.join(""))));e.append(x);
 const c=$("section");c.append($("h2","","拖码越多，概率和成本一起涨"),table(["1 胆","注数","成本","概率","平均返还"],g.plans.map(p=>[p.tuo+" 拖",p.notes,p.cost+" 元",p.prob+"%",p.ret+" 元"])));
 const b=$("section");b.append($("h2","","最新开奖 · "+g.lastDate),digits(g.last));
 const d=$("section");d.append($("h2","","历史统计"),stats([["数据期数",g.total+" 期（自 "+g.first+"）"],["组六占比",g.shares.six+"%（理论 72%）"],["组三占比",g.shares.three+"%（理论 27%）"],["豹子占比",g.shares.triple+"%（理论 1%）"],["随机胆拖回测中奖率",g.hitRate+"%（理论 "+p.prob+"%）"]]));
 v.append(a,e,c,b,d,note("胆拖等于把多注打包：中奖概率随注数上升，成本同比上升，平均返还比例不变。按基本奖金约为投入的 52%，临时派奖期间会高一些，但仍低于投入。胆码和拖码每天随机生成。"))
}
function d3Single(v){
 const g=DATA.d3;if(!g)return empty(v);
 const a=$("section");a.append($("h2","","今日号码 · 单选"));
 g.single.forEach((t,i)=>{const w=$("div","ticket");w.append($("div","label","第 "+(i+1)+" 注"),digits(t));a.append(w)});
 const c=$("section");c.append($("h2","","这一注的情况"),stats([["规则","三位数字和顺序全对"],["成本",g.cost+" 元"],["中奖概率","0.1%"],["中奖可得",g.singlePrize+" 元"]]));
 const b=$("section");b.append($("h2","","最新开奖 · "+g.lastDate),digits(g.last));
 v.append(a,c,b,note("号码由程序随机生成，不能提高中奖概率。请把彩票当作娱乐，并控制预算。"))
}
function lotto(v,key){
 const g=DATA.games[key];if(!g)return empty(v);
 const a=$("section");a.append($("h2","","今日号码"));
 g.tickets.forEach((t,i)=>{const w=$("div","ticket");w.append($("div","label","第 "+(i+1)+" 注"),balls(t.main,t.extra));a.append(w)});
 const b=$("section");b.append($("h2","","最新开奖 · "+g.lastDate),balls(g.lastMain,g.lastExtra));
 let sp=null;
 if(g.strategies){const t=g.strategies,bt=t.backtest,r=x=>[x.average,x.threePlus+"%"];sp=$("section");
  sp.append($("h2","","特殊策略"),$("div","label","冷热搭配"),balls(t.mix.main,t.mix.extra),$("div","label mt","最不可能（偏冷、估计少有人选）"),balls(t.unlikely.main,t.unlikely.extra),$("div","mt"),
  table(["逐期回测","平均命中","中 3 个以上"],[["冷热搭配",...r(bt.mix)],["最不可能",...r(bt.unlikely)],["完全随机",...r(bt.random)]]),
  $("p","sub","这两组不代表中奖概率更高。“最不可能”的作用只是万一中高奖时，可能少一些人分奖。"))}
 const c=$("section");c.append($("h2","","历史统计"),stats([
  ["数据期数",g.total+" 期（自 "+g.first+"）"],
  ["出现最多",g.hot.map(x=>x.n+"（"+x.c+"）").join("、")],
  ["出现最少",g.cold.map(x=>x.n+"（"+x.c+"）").join("、")],
  ["理论次数",g.expected+" 次"],
  ["均匀性检验 p 值",g.p],
  ["最常见奇数个数",g.oddMost.odd+" 个（"+g.oddMost.share+"%）"],
  ["中间 80% 和值",g.sumRange.join(" 至 ")]]));
 const d=$("section");d.append($("h2","","回测：过滤选号 vs 随机选号"),stats([
  ["平均命中（过滤）",g.backtest.filtered.average],
  ["平均命中（随机）",g.backtest.random.average],
  ["命中 3 个以上（过滤）",g.backtest.filtered.threePlus+"%"],
  ["命中 3 个以上（随机）",g.backtest.random.threePlus+"%"]]));
 v.append(...[a,sp,b,c,d].filter(Boolean),note("号码由程序随机生成并按形态过滤，不能提高中奖概率。请把彩票当作娱乐，并控制预算。"))
}
function render(){
 document.querySelectorAll("#modes button").forEach(b=>b.classList.toggle("on",b.dataset.m===state.mode));
 const tabs=document.getElementById("tabs");tabs.hidden=state.mode!=="single";tabs.style.display=tabs.hidden?"none":"";
 document.querySelectorAll("#tabs button").forEach(b=>b.classList.toggle("on",b.dataset.k===state.key));
 try{localStorage.setItem("mode",state.mode);localStorage.setItem("tab",state.key)}catch(e){}
 const v=document.getElementById("view");v.replaceChildren();
 if(state.mode==="dantuo")dantuo(v);else if(state.key==="d3")d3Single(v);else lotto(v,state.key);
 scrollTo(0,0)
}
document.getElementById("demo").hidden=!DATA.demo;
document.getElementById("updated").textContent="更新于 "+DATA.updated;
const tabs=document.getElementById("tabs");
DATA.order.forEach(([k,label])=>{const b=$("button","",label);b.dataset.k=k;b.onclick=()=>{state.key=k;render()};tabs.append(b)});
document.querySelectorAll("#modes button").forEach(b=>b.onclick=()=>{state.mode=b.dataset.m;render()});
render();
if("serviceWorker"in navigator&&location.protocol==="https:")navigator.serviceWorker.register("sw.js").catch(()=>{});
</script></body></html>
"""

WORKER = """const C='daily-numbers';
self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;
e.respondWith(fetch(e.request).then(r=>{const c=r.clone();caches.open(C).then(x=>x.put(e.request,c));return r}).catch(()=>caches.match(e.request)))});
"""


def build_site(folder, games_data, d3, demo):
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "demo": demo, "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "order": [[key, game["name"]] for key, game in GAMES.items()] + [["d3", "福彩3D"]],
        "games": games_data, "d3": d3,
    }
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    (folder / "index.html").write_text(PAGE.replace("__DATA__", data), encoding="utf-8")
    (folder / "sw.js").write_text(WORKER, encoding="utf-8")
    manifest = {
        "name": "每日号码", "short_name": "每日号码", "start_url": "./index.html", "display": "standalone",
        "background_color": "#1d4e89", "theme_color": "#1d4e89",
        "icons": [{"src": f"icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png"} for s in (192, 512)],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for size in (192, 512):
        (folder / f"icon-{size}.png").write_bytes(make_icon(size))


def main():
    parser = argparse.ArgumentParser(description="彩票数据更新与手机网页生成")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    today = date.today().isoformat()
    games_data = {}
    failed = []
    for index, (key, game) in enumerate(GAMES.items()):
        if args.demo:
            draws = demo_draws(game, index)
        else:
            path = Path("data") / f"{key}.csv"
            draws = load(path, game)
            start = (
                date.fromisoformat(max(draws)) - timedelta(days=7) if draws
                else date.today() - timedelta(days=365 * args.years)
            )
            if game["source"] not in FETCHERS:
                print(f"{game['name']}:暂未接入自动数据源,现有 {len(draws)} 期")
                if draws:
                    games_data[key] = summarize(game, draws, today)
                continue
            try:
                before = len(draws)
                draws.update(FETCHERS[game["source"]](game, start))
                save(path, game, draws)
                print(f"{game['name']}:新增 {len(draws) - before} 期,共 {len(draws)} 期")
            except Exception as error:  # 单个彩种失败时,其余彩种继续
                failed.append(game["name"])
                print(f"{game['name']}:下载失败({type(error).__name__}: {error})")
        if draws:
            games_data[key] = summarize(game, draws, today)
    if args.demo:
        rng = random.Random(99)
        d3_draws = {
            (date.today() - timedelta(days=i)).isoformat(): [rng.randrange(10) for _ in range(3)]
            for i in range(800)
        }
    else:
        path = Path("data") / "fc3d.csv"
        d3_draws = load_d3(path)
        start = (
            date.fromisoformat(max(d3_draws)) - timedelta(days=7) if d3_draws
            else date.today() - timedelta(days=365 * args.years)
        )
        if not D3_AUTO:
            print(f"福彩3D:暂未接入自动数据源,现有 {len(d3_draws)} 期")
        else:
            try:
                before = len(d3_draws)
                d3_draws.update(fetch_d3(start))
                save_d3(path, d3_draws)
                print(f"福彩3D:新增 {len(d3_draws) - before} 期,共 {len(d3_draws)} 期")
            except Exception as error:
                failed.append("福彩3D")
                print(f"福彩3D:下载失败({type(error).__name__}: {error})")
    d3 = summarize_d3(d3_draws, today) if d3_draws else None
    build_site(Path("site_demo" if args.demo else "site"), games_data, d3, args.demo)
    print("已生成网页:", "site_demo/index.html" if args.demo else "site/index.html")
    if failed:
        print("未更新:", "、".join(failed), "。请不要反复重试,把上面的报错发给我。")


if __name__ == "__main__":
    main()
