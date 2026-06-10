"""オッズドリフト (締切直前の下振れ) と自票インパクトの定量化。

データ源:
- data/cache/odds_timeline/*.jsonl : score/bet/poll 段の時系列オッズ (全組合せ)
- data/results/*.json              : final_odds (確定オッズ, 束の脚 + 当たり組合せ。
                                     一部 JRA は全組合せダンプ)
- data/predictions/*.json          : snapshot 保存時オッズ (bet_tables / rows)

計測:
 1. 券種別 final/bet オッズ比の分布 (NAR vs JRA)
 2. オッズ帯別ドリフト
 3. NAR pool 規模の逆算 (3連単 max odds → pool ≈ O_max*100/rate) と自票インパクト
 4. TORIGAMI_MARGIN の券種別提案
 5. late-money シグナル (score→bet で単勝が下がった馬は勝ちやすいか)

読み取り専用。既存コードは変更しない。
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import math
import os
import statistics as st
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TL_DIR = os.path.join(ROOT, "data", "cache", "odds_timeline")
RES_DIR = os.path.join(ROOT, "data", "results")
PRED_DIR = os.path.join(ROOT, "data", "predictions")

JRA_VENUES = {"札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"}

# 払戻率 (JRA 公称)。NAR は主催者により ±数% 異なる点に注意。
PAYOUT_RATE = {
    "win": 0.80, "place": 0.80, "quinella": 0.775, "wide": 0.775,
    "exacta": 0.75, "trio": 0.75, "trifecta": 0.725,
}

BT_JA = {
    "win": "単勝", "place": "複勝", "quinella": "馬連", "wide": "ワイド",
    "exacta": "馬単", "trio": "3連複", "trifecta": "3連単",
}


def load_timelines() -> dict[str, list[dict]]:
    out = {}
    for f in glob.glob(os.path.join(TL_DIR, "*.jsonl")):
        rid = os.path.basename(f)[:-6]
        lines = []
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    lines.append(json.loads(line))
        if lines:
            lines.sort(key=lambda x: x.get("captured_at", ""))
            out[rid] = lines
    return out


def load_results() -> dict[str, dict]:
    out = {}
    for f in glob.glob(os.path.join(RES_DIR, "*.json")):
        rid = os.path.basename(f)[:-5]
        out[rid] = json.load(open(f, encoding="utf-8"))
    return out


def load_predictions() -> dict[str, dict]:
    out = {}
    for f in glob.glob(os.path.join(PRED_DIR, "*.json")):
        if f.endswith(".llm.json"):
            continue
        rid = os.path.basename(f)[:-5]
        try:
            out[rid] = json.load(open(f, encoding="utf-8"))
        except Exception:
            pass
    return out


def classify(rid: str, preds: dict, results: dict) -> str:
    p = preds.get(rid)
    if p:
        if p.get("odds_source") == "jra" or p.get("venue_name") in JRA_VENUES:
            return "JRA"
        return "NAR"
    r = results.get(rid) or {}
    src = r.get("source", "")
    if src == "jra":
        return "JRA"
    if src in ("keibago", "oddspark"):
        return "NAR"
    return "?"


def ts(iso: str) -> float:
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fmt(x, nd=3):
    return "nan" if (x is None or (isinstance(x, float) and math.isnan(x))) else f"{x:.{nd}f}"


def winning_keys(finish: list[int]) -> dict[str, set[str]]:
    if not finish or len(finish) < 3:
        return {}
    a, b, c = finish[0], finish[1], finish[2]
    return {
        "win": {str(a)},
        "place": {str(a), str(b), str(c)},
        "quinella": {"-".join(map(str, sorted((a, b))))},
        "wide": {"-".join(map(str, sorted(p))) for p in ((a, b), (a, c), (b, c))},
        "exacta": {f"{a}-{b}"},
        "trio": {"-".join(map(str, sorted((a, b, c))))},
        "trifecta": {f"{a}-{b}-{c}"},
    }


def odds_band(o: float) -> str:
    if o < 10:
        return "<10"
    if o < 50:
        return "10-50"
    if o < 200:
        return "50-200"
    if o < 1000:
        return "200-1000"
    return "1000+"


BANDS = ["<10", "10-50", "50-200", "200-1000", "1000+"]


def dist_row(xs):
    return dict(n=len(xs), mean=st.mean(xs) if xs else float("nan"),
                median=pct(xs, 0.5), p25=pct(xs, 0.25), p10=pct(xs, 0.10),
                p5=pct(xs, 0.05),
                breach110=sum(1 for x in xs if x < 1 / 1.10) / len(xs) if xs else float("nan"))


def print_dist_table(title, rows):
    print(f"\n### {title}")
    print(f"{'group':<28}{'n':>6}{'mean':>8}{'med':>8}{'p25':>8}{'p10':>8}{'p5':>8}{'<1/1.1':>8}")
    for name, xs in rows:
        d = dist_row(xs)
        print(f"{name:<28}{d['n']:>6}{fmt(d['mean']):>8}{fmt(d['median']):>8}"
              f"{fmt(d['p25']):>8}{fmt(d['p10']):>8}{fmt(d['p5']):>8}{fmt(d['breach110']):>8}")


def main():
    tls = load_timelines()
    results = load_results()
    preds = load_predictions()
    cls = {rid: classify(rid, preds, results) for rid in set(tls) | set(results) | set(preds)}

    # ---------------- 1) final/bet 比 (timeline 最終行 vs results.final_odds) ----------
    # ratio レコード: (bt, cls, bet_odds, ratio, is_winning, lead_sec, rid)
    recs_tl = []
    lead_secs = []
    for rid, lines in tls.items():
        res = results.get(rid)
        if not res or not res.get("final_odds"):
            continue
        last = lines[-1]
        close = last.get("close_at") or 0
        cap = ts(last.get("captured_at", ""))
        lead = (close - cap) if (close and cap) else float("nan")
        if not math.isnan(lead):
            lead_secs.append(lead)
        wk = winning_keys(res.get("finish_order") or [])
        for key, fo in (res["final_odds"] or {}).items():
            bt, label = key.split(":", 1)
            bet_o = (last.get("odds") or {}).get(bt, {}).get(label)
            if not bet_o or not fo:
                continue
            is_win = label in wk.get(bt, set())
            recs_tl.append((bt, cls.get(rid, "?"), bet_o, fo / bet_o, is_win, lead, rid))

    print("=" * 90)
    print("1) final / bet時 オッズ比 — timeline 最終行 (締切直前) vs 確定オッズ")
    print(f"   join できたレース: {len({r[6] for r in recs_tl})} / timeline {len(tls)} files")
    if lead_secs:
        print(f"   timeline 最終行の締切までの lead: median {pct(lead_secs,0.5):.0f}s "
              f"p25 {pct(lead_secs,0.25):.0f}s p75 {pct(lead_secs,0.75):.0f}s")

    for c in ("NAR", "JRA"):
        rows = []
        for bt in BT_JA:
            xs = [r[3] for r in recs_tl if r[0] == bt and r[1] == c]
            if xs:
                rows.append((f"{BT_JA[bt]} ({bt})", xs))
        print_dist_table(f"{c}: 券種別 final/bet 比 (全 matched keys = 束の脚+当たり組合せ)", rows)

    # 当たり組合せのみ (payout に直結する条件付き分布)
    for c in ("NAR", "JRA"):
        rows = []
        for bt in BT_JA:
            xs = [r[3] for r in recs_tl if r[0] == bt and r[1] == c and r[4]]
            if xs:
                rows.append((f"{BT_JA[bt]} ({bt})", xs))
        print_dist_table(f"{c}: 当たり組合せのみの final/bet 比", rows)

    # ---------------- 1b) snapshot (predictions) vs final — n を稼ぐ補完 -------------
    recs_sn = []
    for rid, p in preds.items():
        res = results.get(rid)
        if not res or not res.get("final_odds"):
            continue
        close = p.get("close_at") or 0
        saved = ts(p.get("saved_at", ""))
        lead = (close - saved) if (close and saved) else float("nan")
        if math.isnan(lead) or not (0 <= lead <= 600):  # 締切10分前以内の snapshot のみ
            continue
        c = cls.get(rid, "?")
        wk = winning_keys(res.get("finish_order") or [])
        # 単勝: bet_tables.win は全馬
        snap_odds: dict[str, dict[str, float]] = defaultdict(dict)
        for bt, tbl in (p.get("bet_tables") or {}).items():
            for row in tbl or []:
                k = row.get("key") or []
                o = row.get("odds")
                if not o:
                    continue
                if bt in ("win", "place"):
                    label = str(k[0])
                elif bt in ("quinella", "wide", "trio"):
                    label = "-".join(map(str, sorted(k)))
                else:
                    label = "-".join(map(str, k))
                snap_odds[bt][label] = o
        for row in p.get("rows") or []:  # 3連単 全組合せ
            k = row.get("key") or []
            o = row.get("odds")
            if o and len(k) == 3:
                snap_odds["trifecta"]["-".join(map(str, k))] = o
        for key, fo in (res["final_odds"] or {}).items():
            bt, label = key.split(":", 1)
            bo = snap_odds.get(bt, {}).get(label)
            if not bo or not fo:
                continue
            recs_sn.append((bt, c, bo, fo / bo, label in wk.get(bt, set()), lead, rid))

    print("\n" + "=" * 90)
    print("1b) final / snapshot保存時 オッズ比 (snapshot は締切≤10分前のみ採用)")
    print(f"    join レース数: {len({r[6] for r in recs_sn})}")
    for c in ("NAR", "JRA"):
        rows = []
        for bt in BT_JA:
            xs = [r[3] for r in recs_sn if r[0] == bt and r[1] == c]
            if xs:
                rows.append((f"{BT_JA[bt]} ({bt})", xs))
        print_dist_table(f"{c}: 券種別 final/snapshot 比", rows)

    # ---------------- 2) オッズ帯別ドリフト ------------------------------------------
    print("\n" + "=" * 90)
    print("2) オッズ帯別ドリフト (bet時オッズの帯ごと)")
    both = recs_tl + recs_sn
    for c in ("NAR", "JRA"):
        rows = []
        for b in BANDS:
            xs = [r[3] for r in both if r[1] == c and odds_band(r[2]) == b]
            if xs:
                rows.append((f"band {b}", xs))
        print_dist_table(f"{c}: 全券種込み final/bet(snapshot) 比 帯別", rows)
    # 3連単のみの帯別 (主力券種)
    for c in ("NAR", "JRA"):
        rows = []
        for b in BANDS:
            xs = [r[3] for r in both if r[0] == "trifecta" and r[1] == c and odds_band(r[2]) == b]
            if xs:
                rows.append((f"band {b}", xs))
        print_dist_table(f"{c}: 3連単のみ 帯別", rows)

    # score→bet (timeline 内, 全組合せ, 当たり選択バイアス無し)
    recs_sb = []
    for rid, lines in tls.items():
        if len(lines) < 2:
            continue
        first, last = lines[0], lines[-1]
        dt_min = (ts(last["captured_at"]) - ts(first["captured_at"])) / 60
        c = cls.get(rid, "?")
        for bt, d0 in (first.get("odds") or {}).items():
            d1 = (last.get("odds") or {}).get(bt) or {}
            for label, o0 in d0.items():
                o1 = d1.get(label)
                if o0 and o1:
                    recs_sb.append((bt, c, o0, o1 / o0, dt_min, rid))
    print("\n--- 参考: score段→bet段 (約4-5分間) の全組合せドリフト (当たり選択バイアス無し) ---")
    for c in ("NAR", "JRA"):
        rows = []
        for b in BANDS:
            xs = [r[3] for r in recs_sb if r[1] == c and r[0] == "trifecta" and odds_band(r[2]) == b]
            if xs:
                rows.append((f"3連単 band {b}", xs))
        print_dist_table(f"{c}: score→bet 比 (3連単, 帯別)", rows)

    # ---------------- 3) pool 規模逆算と自票インパクト ---------------------------------
    print("\n" + "=" * 90)
    print("3) 3連単 pool 規模の逆算 (pool ≈ O_max × 100 / 払戻率0.725, 最薄組=100円仮定)")
    pools = defaultdict(list)
    int_checks = []
    for rid, lines in tls.items():
        last = lines[-1]
        tri = (last.get("odds") or {}).get("trifecta") or {}
        if len(tri) < 20:
            continue
        omax = max(tri.values())
        pool = omax * 100 / 0.725
        pools[cls.get(rid, "?")].append((rid, omax, pool, len(tri)))
        # 整数チェック: O_max/O_i が整数に近いか (上位=大きいオッズ側で)
        top = sorted(tri.values(), reverse=True)[:30]
        for o in top:
            k = omax / o
            if k >= 1.5:
                int_checks.append(abs(k - round(k)))
    for c in ("NAR", "JRA"):
        ps = sorted(p for _, _, p, _ in pools[c])
        if not ps:
            continue
        print(f"  {c}: races={len(ps)} pool 推定 median ¥{pct(ps,0.5):,.0f} "
              f"p25 ¥{pct(ps,0.25):,.0f} p75 ¥{pct(ps,0.75):,.0f} "
              f"min ¥{ps[0]:,.0f} max ¥{ps[-1]:,.0f}")
    if int_checks:
        ok = sum(1 for e in int_checks if e < 0.15) / len(int_checks)
        print(f"  整数チェック (O_max/O_i, k≥1.5 の {len(int_checks)} 点): "
              f"|k-round(k)|<0.15 の割合 = {ok:.1%} (高いほど 100円最小単位仮定が妥当)")

    # NAR pool 例の表示
    nar_pools = sorted(pools["NAR"], key=lambda x: x[2])
    if nar_pools:
        print("  NAR pool 推定の例 (小さい順5件):")
        for rid, omax, pool, ntri in nar_pools[:5]:
            print(f"    {rid}: O_max={omax:,.0f} → pool≈¥{pool:,.0f} (組合せ数 {ntri})")

    print("\n  自票インパクト: 1点に s 円入れたときの新オッズ比 O'/O = (1+s/P)/(1+s·O/(R·P))")
    print("  (R=0.725, P=pool, O=現在オッズ)")
    nar_ps = sorted(p for _, _, p, _ in pools["NAR"])
    jra_ps = sorted(p for _, _, p, _ in pools["JRA"])
    scenarios = []
    if nar_ps:
        scenarios += [("NAR p25", pct(nar_ps, 0.25)), ("NAR median", pct(nar_ps, 0.5)),
                      ("NAR p75", pct(nar_ps, 0.75))]
    if jra_ps:
        scenarios += [("JRA median", pct(jra_ps, 0.5))]
    hdr = f"{'pool':<24}{'stake':>7}" + "".join(f"{'O='+str(o):>10}" for o in (50, 200, 1000, 5000))
    print("  " + hdr)
    R = 0.725
    for name, P in scenarios:
        for s in (500, 1000, 3000, 5000):
            cells = []
            for O in (50, 200, 1000, 5000):
                ratio = (1 + s / P) / (1 + s * O / (R * P))
                cells.append(f"{ratio:>10.3f}")
            print(f"  {name+f' ¥{P:,.0f}':<24}{s:>7}" + "".join(cells))

    # ---------------- 4) margin 提案 ---------------------------------------------------
    print("\n" + "=" * 90)
    print("4) TORIGAMI_MARGIN 提案 (margin = 1/分位点。当たり組合せ時に払戻≥投資を保証したい確率で選ぶ)")
    print(f"{'group':<34}{'n':>6}{'1/med':>8}{'1/p25':>8}{'1/p10':>8}{'1/p5':>8}")
    for c in ("NAR", "JRA"):
        for bt in BT_JA:
            xs = [r[3] for r in both if r[0] == bt and r[1] == c]
            if len(xs) < 8:
                continue
            print(f"{c+' '+BT_JA[bt]:<34}{len(xs):>6}"
                  f"{fmt(1/pct(xs,0.5),2):>8}{fmt(1/pct(xs,0.25),2):>8}"
                  f"{fmt(1/pct(xs,0.10),2):>8}{fmt(1/pct(xs,0.05),2):>8}")

    # ---------------- 5) late-money シグナル -------------------------------------------
    print("\n" + "=" * 90)
    print("5) late-money: score→bet で単勝が下がった馬はその後勝ちやすいか")
    n_races = 0
    winner_ranks = []          # 勝ち馬の drift 順位 percentile (0=最も売れた)
    groups = {"shortened": [0, 0.0, 0.0], "drifted": [0, 0.0, 0.0]}  # [bets, wins, payout]
    for rid, lines in tls.items():
        res = results.get(rid)
        if not res or len(lines) < 2:
            continue
        finish = res.get("finish_order") or []
        if not finish:
            continue
        w0 = (lines[0].get("odds") or {}).get("win") or {}
        w1 = (lines[-1].get("odds") or {}).get("win") or {}
        common = [h for h in w0 if h in w1]
        if len(common) < 5:
            continue
        n_races += 1
        drift = {h: w1[h] / w0[h] for h in common}
        winner = str(finish[0])
        if winner in drift:
            order = sorted(common, key=lambda h: drift[h])
            rank = order.index(winner)
            winner_ranks.append(rank / (len(common) - 1))
        fo_win = {k.split(":")[1]: v for k, v in (res.get("final_odds") or {}).items()
                  if k.startswith("win:")}
        for h in common:
            g = "shortened" if drift[h] < 1.0 else "drifted"
            groups[g][0] += 1
            if h == winner:
                groups[g][1] += 1
                groups[g][2] += fo_win.get(h, w1[h])  # 100円当たり払戻 (倍率)
    print(f"  対象レース: {n_races} (timeline 2行以上 + 結果あり)")
    if winner_ranks:
        med = pct(winner_ranks, 0.5)
        mean = st.mean(winner_ranks)
        # 勝ち馬の drift percentile が 0.5 未満 (= 売れた側) のレース数
        below = sum(1 for x in winner_ranks if x < 0.5)
        n = len(winner_ranks)
        # 二項検定 (正規近似)
        z = (below - n / 2) / math.sqrt(n / 4)
        print(f"  勝ち馬の late-drift 順位 percentile: mean {mean:.3f} median {med:.3f} "
              f"(0.5未満={below}/{n}, z={z:+.2f})")
    for g, (b, w, pay) in groups.items():
        if b:
            roi = pay / b  # 1単位賭け→倍率回収
            print(f"  {g:<10}: bets={b:>4} wins={w:>3} hit={w/b:.3%} flat単勝ROI={roi:.1%}")

    # ---------------- 補足: 束の脚 (非当たり) vs 当たり組合せのドリフト差 ----------------
    print("\n--- 補足: 3連単 final/bet 比 — 束の脚(外れ) vs 当たり組合せ (NAR) ---")
    legs = [r[3] for r in recs_tl if r[0] == "trifecta" and r[1] == "NAR" and not r[4]]
    wins = [r[3] for r in recs_tl if r[0] == "trifecta" and r[1] == "NAR" and r[4]]
    print_dist_table("NAR 3連単", [("束の脚 (外れ含む)", legs), ("当たり組合せ", wins)])


if __name__ == "__main__":
    main()
