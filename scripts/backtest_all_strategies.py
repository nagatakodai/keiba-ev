#!/usr/bin/env python3
"""全戦略の実 ROI / 的中率 計測 (snapshot × results 突合バックテスト)。

読み取り専用 — 既存コードは変更しない。

計測対象:
  1. recommended_bundle_t (3連単束):  mode / rank_source / 日付 / JRA-NAR 別
  2. recommended_bundle  (EV束 joint Kelly): 券種別の外れ方分解, torigami 実態
  3. 仮想戦略: model top-1 単勝 / Claude 指数 top-1 単勝 / 市場 top-1 単勝 /
     model top-1 複勝 / model top-3 ボックス ワイド・馬連
  4. 較正: bet_tables.win の予測1着確率 bin vs 実勝率, llm_win_index softmax 同様

的中時の実払戻は result.final_odds (確定オッズ表) を優先、無ければ
trifecta_payout (3連単のみ, 100円あたり) → snapshot 保存時オッズの順で fallback。

使い方:
    .venv/bin/python scripts/backtest_all_strategies.py [--json]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRED_DIR = ROOT / "data" / "predictions"
RES_DIR = ROOT / "data" / "results"

JRA_VENUES = {"札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"}

T_LLM = 25.0  # ev.py の T_LLM (softmax 温度)


# ─────────────────────────── util ───────────────────────────


def wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def race_date(snap: dict) -> str:
    sa = snap.get("start_at")
    if sa:
        try:
            return datetime.fromtimestamp(int(sa)).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            pass
    s = snap.get("saved_at") or ""
    return s[:10] if s else "unknown"


def is_jra(snap: dict) -> bool:
    return (snap.get("venue_name") or "") in JRA_VENUES


def fo_key(nums: list[int], ordered: bool) -> str:
    if ordered:
        return "-".join(str(int(x)) for x in nums)
    return "-".join(str(x) for x in sorted(int(x) for x in nums))


def n_places(n_horses: int) -> int:
    # JRA/NAR とも 8 頭未満は複勝 2 着まで (7 頭以下)
    return 2 if (n_horses and n_horses <= 7) else 3


# ─────────────────────────── leg 判定 ───────────────────────────


def leg_hit_and_final_odds(
    leg: dict, fo: list[int], final_odds: dict, trifecta_payout: float | None, n_horses: int
) -> tuple[bool, float | None, str]:
    """(hit, final_odds_value(オッズ倍率) or None, note)。final_odds が無い hit は snap odds fallback。"""
    bt = leg.get("bet_type")
    key = [int(k) for k in (leg.get("key") or [])]
    top3 = [int(x) for x in fo[:3]]
    fset = set(top3)
    note = ""
    if bt == "win":
        hit = key[0] == top3[0]
        fo_v = final_odds.get(f"win:{key[0]}") if hit else None
    elif bt == "place":
        # 【2026-06-11 修正】hit は**着順+頭数ベース**で判定する。旧実装の
        # 「final_odds にキーが在れば的中」は netkeiba result (払戻=当たり組のみ) 前提で、
        # keibago/jra fallback result は final_odds に**全組合せ**を保存するため
        # 全脚が的中扱い (実測 57 脚誤 hit / EV束 ROI +52.6pt 上振れ) になっていた。
        # final_odds は的中脚のオッズ lookup のみに使う。
        hit = key[0] in set(fo[: n_places(n_horses)])
        fo_v = final_odds.get(f"place:{key[0]}") if hit else None
    elif bt == "wide":
        hit = set(key) <= fset
        fo_v = final_odds.get(f"wide:{fo_key(key, False)}") if hit else None
    elif bt == "quinella":
        hit = set(key) == set(top3[:2])
        fo_v = final_odds.get(f"quinella:{fo_key(key, False)}") if hit else None
    elif bt == "exacta":
        hit = key == top3[:2]
        fo_v = final_odds.get(f"exacta:{fo_key(key, True)}") if hit else None
    elif bt == "trio":
        hit = set(key) == fset
        fo_v = final_odds.get(f"trio:{fo_key(key, False)}") if hit else None
    elif bt == "trifecta":
        hit = key == top3
        fo_v = final_odds.get(f"trifecta:{fo_key(key, True)}") if hit else None
        if hit and fo_v is None and trifecta_payout:
            fo_v = trifecta_payout / 100.0
            note = "trifecta_payout fallback"
    else:
        return False, None, f"unknown bet_type {bt}"
    return bool(hit), fo_v, note


def eval_bundle(bundle: dict, fo: list[int], final_odds: dict, trifecta_payout, n_horses: int) -> dict:
    """束 (legs) の stake/return を snapshot odds と final odds の両方で計算。"""
    legs = bundle.get("legs") or []
    stake = 0
    ret_final = 0.0
    ret_snap = 0.0
    hit_legs = []
    by_type: dict[str, dict] = {}
    fallback_snap_odds = 0
    for leg in legs:
        st = int(leg.get("stake") or 0)
        if st <= 0:
            continue
        stake += st
        bt = leg.get("bet_type") or "?"
        d = by_type.setdefault(bt, {"stake": 0, "ret_final": 0.0, "hits": 0, "legs": 0})
        d["stake"] += st
        d["legs"] += 1
        hit, fov, note = leg_hit_and_final_odds(leg, fo, final_odds, trifecta_payout, n_horses)
        if hit:
            snap_odds = float(leg.get("odds") or 0.0)
            if fov is None:
                fov_use = snap_odds
                fallback_snap_odds += 1
            else:
                fov_use = float(fov)
            ret_final += st * fov_use
            ret_snap += st * snap_odds
            d["ret_final"] += st * fov_use
            d["hits"] += 1
            hit_legs.append(
                {
                    "bet_type": bt,
                    "key": leg.get("key"),
                    "stake": st,
                    "snap_odds": snap_odds,
                    "final_odds": fov,
                    "drift": (float(fov) / snap_odds) if (fov and snap_odds > 0) else None,
                }
            )
    return {
        "stake": stake,
        "ret_final": ret_final,
        "ret_snap": ret_snap,
        "n_legs": len([l for l in legs if (l.get("stake") or 0) > 0]),
        "hit_legs": hit_legs,
        "by_type": by_type,
        "fallback_snap_odds": fallback_snap_odds,
    }


# ─────────────────────────── main ───────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    snaps = {}
    for p in sorted(glob.glob(str(PRED_DIR / "*.json"))):
        if p.endswith(".llm.json"):
            continue
        rid = os.path.basename(p)[:-5]
        d = _load(Path(p))
        if d:
            snaps[rid] = d
    results = {}
    for p in sorted(glob.glob(str(RES_DIR / "*.json"))):
        rid = os.path.basename(p)[:-5]
        d = _load(Path(p))
        if d:
            results[rid] = d

    joined = []
    for rid, snap in snaps.items():
        res = results.get(rid)
        if not res:
            continue
        fo = res.get("finish_order") or []
        if len(fo) < 3 or not res.get("trifecta_payout"):
            continue
        joined.append((rid, snap, res))

    print("=" * 90)
    print("全戦略バックテスト (snapshot × results 突合)")
    print("=" * 90)
    print(f"snapshots={len(snaps)}  results={len(results)}  join (finish_order>=3 & trifecta_payout): N={len(joined)}")
    n_final_odds = sum(1 for _, _, r in joined if r.get("final_odds"))
    print(f"  うち final_odds (確定払戻表) あり: {n_final_odds}")
    n_jra = sum(1 for _, s, _ in joined if is_jra(s))
    print(f"  JRA: {n_jra}  NAR: {len(joined) - n_jra}")

    out: dict = {"n_joined": len(joined), "n_final_odds": n_final_odds}

    # ════════════════ 1. recommended_bundle_t (3連単束) ════════════════
    print("\n" + "=" * 90)
    print("1. recommended_bundle_t (3連単束, 実弾投票対象)")
    print("=" * 90)

    bt_rows = []
    for rid, snap, res in joined:
        b = snap.get("recommended_bundle_t")
        if not b or not (b.get("legs") or []):
            continue
        fo = res["finish_order"]
        n_horses = max(len(snap.get("market_signals") or []), len((snap.get("bet_tables") or {}).get("win") or []))
        ev = eval_bundle(b, fo, res.get("final_odds") or {}, res.get("trifecta_payout"), n_horses)
        if ev["stake"] <= 0:
            continue
        bt_rows.append(
            {
                "rid": rid,
                "date": race_date(snap),
                "jra": is_jra(snap),
                "mode": b.get("mode") or "hit(欠落)",
                "rank_source": b.get("rank_source") or "?",
                **ev,
            }
        )

    def agg(rows, label):
        n = len(rows)
        stake = sum(r["stake"] for r in rows)
        retf = sum(r["ret_final"] for r in rows)
        rets = sum(r["ret_snap"] for r in rows)
        hit_races = sum(1 for r in rows if r["hit_legs"])
        lo, hi = wilson_ci(hit_races, n)
        roi_f = retf / stake * 100 if stake else 0.0
        roi_s = rets / stake * 100 if stake else 0.0
        print(
            f"  {label:<28} n={n:3d} 的中R={hit_races:2d} ({hit_races/n*100 if n else 0:5.1f}%, "
            f"Wilson95% [{lo*100:4.1f},{hi*100:5.1f}])  stake=¥{stake:,}  "
            f"払戻(final)=¥{retf:,.0f}  ROI(final)={roi_f:6.1f}%  ROI(snap odds)={roi_s:6.1f}%"
        )
        return {
            "label": label, "n": n, "hit_races": hit_races, "stake": stake,
            "return_final": retf, "roi_final_pct": roi_f, "roi_snap_pct": roi_s,
            "wilson95": [lo, hi],
        }

    out["bundle_t"] = {}
    print(f"\n[全体]  束あり snapshot との join: {len(bt_rows)} レース")
    out["bundle_t"]["all"] = agg(bt_rows, "ALL")
    out["bundle_t"]["claude_gate"] = agg(
        [r for r in bt_rows if r["rank_source"] == "claude"], "rank_source=claude (実投票対象)"
    )
    out["bundle_t"]["model"] = agg([r for r in bt_rows if r["rank_source"] != "claude"], "rank_source=model (見送り扱い)")

    print("\n[mode 別]")
    out["bundle_t"]["by_mode"] = {}
    for mode in sorted({r["mode"] for r in bt_rows}):
        out["bundle_t"]["by_mode"][mode] = agg([r for r in bt_rows if r["mode"] == mode], f"mode={mode}")

    print("\n[mode × rank_source=claude のみ]")
    for mode in sorted({r["mode"] for r in bt_rows}):
        agg([r for r in bt_rows if r["mode"] == mode and r["rank_source"] == "claude"], f"mode={mode} & claude")

    print("\n[JRA / NAR]")
    agg([r for r in bt_rows if r["jra"]], "JRA")
    agg([r for r in bt_rows if not r["jra"]], "NAR")

    print("\n[日付別]")
    out["bundle_t"]["by_date"] = {}
    for dte in sorted({r["date"] for r in bt_rows}):
        out["bundle_t"]["by_date"][dte] = agg([r for r in bt_rows if r["date"] == dte], dte)

    print("\n[的中レース詳細 (3連単束)]")
    drift_list = []
    for r in bt_rows:
        for hl in r["hit_legs"]:
            drift_list.append(hl.get("drift"))
            print(
                f"  {r['rid']:<22} {r['date']} mode={r['mode']:<10} src={r['rank_source']:<6} "
                f"key={hl['key']} stake=¥{hl['stake']} snap_odds={hl['snap_odds']:.1f} "
                f"final_odds={hl['final_odds']} drift(final/snap)={hl['drift'] if hl['drift'] is None else round(hl['drift'],3)}"
            )
    dl = [d for d in drift_list if d]
    if dl:
        dl_s = sorted(dl)
        print(f"  drift(final/snap) median={dl_s[len(dl_s)//2]:.3f}  min={min(dl):.3f}  max={max(dl):.3f}  n={len(dl)}")
        out["bundle_t"]["hit_drift"] = dl

    # ════════════════ 2. recommended_bundle (EV束) ════════════════
    print("\n" + "=" * 90)
    print("2. recommended_bundle (EV束 joint Kelly, 参考値)")
    print("=" * 90)

    ev_rows = []
    for rid, snap, res in joined:
        b = snap.get("recommended_bundle")
        if not b or not (b.get("legs") or []):
            continue
        fo = res["finish_order"]
        n_horses = max(len(snap.get("market_signals") or []), len((snap.get("bet_tables") or {}).get("win") or []))
        ev = eval_bundle(b, fo, res.get("final_odds") or {}, res.get("trifecta_payout"), n_horses)
        if ev["stake"] <= 0:
            continue
        ev_rows.append({"rid": rid, "date": race_date(snap), "jra": is_jra(snap), **ev})

    print(f"\n[全体]  EV束あり snapshot との join: {len(ev_rows)} レース")
    out["ev_bundle"] = {}
    out["ev_bundle"]["all"] = agg(ev_rows, "ALL")
    agg([r for r in ev_rows if r["jra"]], "JRA")
    agg([r for r in ev_rows if not r["jra"]], "NAR")

    # per-race 収支: 的中したが torigami (払戻 < stake) のレース
    hit_races = [r for r in ev_rows if r["hit_legs"]]
    torigami = [r for r in hit_races if r["ret_final"] < r["stake"]]
    profit = [r for r in hit_races if r["ret_final"] >= r["stake"]]
    print(f"\n  的中レース {len(hit_races)} / {len(ev_rows)}: うち torigami (払戻<stake) {len(torigami)}, プラス {len(profit)}")
    out["ev_bundle"]["hit_races"] = len(hit_races)
    out["ev_bundle"]["torigami_races"] = len(torigami)

    print("\n[券種別分解 (EV束)]")
    type_agg: dict[str, dict] = {}
    for r in ev_rows:
        for bt, d in r["by_type"].items():
            t = type_agg.setdefault(bt, {"stake": 0, "ret_final": 0.0, "hits": 0, "legs": 0})
            for k in ("stake", "ret_final", "hits", "legs"):
                t[k] += d[k]
    out["ev_bundle"]["by_type"] = {}
    for bt, t in sorted(type_agg.items(), key=lambda x: -x[1]["stake"]):
        roi = t["ret_final"] / t["stake"] * 100 if t["stake"] else 0
        lo, hi = wilson_ci(t["hits"], t["legs"])
        print(
            f"  {bt:<10} legs={t['legs']:4d} hits={t['hits']:3d} ({t['hits']/t['legs']*100 if t['legs'] else 0:5.1f}%, "
            f"W95[{lo*100:4.1f},{hi*100:5.1f}])  stake=¥{t['stake']:,}  払戻=¥{t['ret_final']:,.0f}  ROI={roi:6.1f}%"
        )
        out["ev_bundle"]["by_type"][bt] = {**t, "roi_pct": roi}

    # hit した EV束レッグの drift
    ev_drifts = [hl["drift"] for r in ev_rows for hl in r["hit_legs"] if hl.get("drift")]
    if ev_drifts:
        s = sorted(ev_drifts)
        print(f"\n  hit レッグ drift(final/snap): n={len(s)} median={s[len(s)//2]:.3f} p10={s[int(len(s)*0.1)]:.3f} p90={s[int(len(s)*0.9)]:.3f}")
        out["ev_bundle"]["hit_drift_median"] = s[len(s) // 2]

    # ════════════════ 3. 仮想戦略 ════════════════
    print("\n" + "=" * 90)
    print("3. 仮想戦略バックテスト (各レース 100 円 flat bet)")
    print("=" * 90)

    def win_table(snap):
        rows = (snap.get("bet_tables") or {}).get("win") or []
        return [
            {"n": int(r["key"][0]), "odds": float(r.get("odds") or 0), "prob": float(r.get("prob") or 0)}
            for r in rows
            if r.get("key")
        ]

    strategies = {
        "model_top1_win": [],
        "claude_top1_win": [],
        "market_top1_win": [],
        "model_top1_place": [],
        "model_box3_wide": [],
        "model_box3_quinella": [],
    }

    for rid, snap, res in joined:
        wt = win_table(snap)
        if not wt:
            continue
        fo = [int(x) for x in res["finish_order"]]
        fodds = res.get("final_odds") or {}
        n_horses = max(len(snap.get("market_signals") or []), len(wt))
        jra = is_jra(snap)

        # a. model top-1 win
        pick = max(wt, key=lambda r: r["prob"])
        hit = pick["n"] == fo[0]
        strategies["model_top1_win"].append(
            {
                "rid": rid, "jra": jra, "hit": hit, "snap_odds": pick["odds"],
                "final": (fodds.get(f"win:{pick['n']}") if hit else 0.0),
                "has_final": bool(fodds),
            }
        )

        # b. claude top-1 win
        llm = snap.get("llm_win_index") or {}
        if llm:
            try:
                top = max(llm.items(), key=lambda kv: float(kv[1]))
                cn = int(top[0])
                entry = next((r for r in wt if r["n"] == cn), None)
                if entry:
                    hit = cn == fo[0]
                    strategies["claude_top1_win"].append(
                        {
                            "rid": rid, "jra": jra, "hit": hit, "snap_odds": entry["odds"],
                            "final": (fodds.get(f"win:{cn}") if hit else 0.0),
                            "has_final": bool(fodds),
                        }
                    )
            except (ValueError, TypeError):
                pass

        # c. market top-1 (最低オッズ)
        mk = min((r for r in wt if r["odds"] > 0), key=lambda r: r["odds"], default=None)
        if mk:
            hit = mk["n"] == fo[0]
            strategies["market_top1_win"].append(
                {
                    "rid": rid, "jra": jra, "hit": hit, "snap_odds": mk["odds"],
                    "final": (fodds.get(f"win:{mk['n']}") if hit else 0.0),
                    "has_final": bool(fodds),
                }
            )

        # d. model top-1 place
        pt = (snap.get("bet_tables") or {}).get("place") or []
        place_odds = {int(r["key"][0]): float(r.get("odds") or 0) for r in pt if r.get("key")}
        if fodds:
            # 着順+頭数ベース判定 (キー存在判定は keibago/jra の全組 final_odds で誤 hit)
            phit = pick["n"] in set(fo[: n_places(n_horses)])
            pfinal = fodds.get(f"place:{pick['n']}", 0.0) if phit else 0.0
        else:
            phit = pick["n"] in set(fo[: n_places(n_horses)])
            pfinal = place_odds.get(pick["n"], 0.0) if phit else 0.0  # snap fallback
        strategies["model_top1_place"].append(
            {
                "rid": rid, "jra": jra, "hit": phit,
                "snap_odds": place_odds.get(pick["n"], 0.0),
                "final": pfinal, "has_final": bool(fodds),
            }
        )

        # e. model top-3 box wide (3 pairs) & quinella (3 pairs) — final odds のみで評価
        top3 = [r["n"] for r in sorted(wt, key=lambda r: -r["prob"])[:3]]
        if len(top3) == 3 and fodds:
            fset = set(fo[:3])
            for a, b in ((0, 1), (0, 2), (1, 2)):
                pair = sorted([top3[a], top3[b]])
                k = f"wide:{pair[0]}-{pair[1]}"
                # 着順ベース判定 (キー存在判定は keibago/jra の全組 final_odds で誤 hit)
                hit = set(pair) <= fset
                strategies["model_box3_wide"].append(
                    {"rid": rid, "jra": jra, "hit": hit, "snap_odds": 0.0,
                     "final": fodds.get(k, 0.0) if hit else 0.0, "has_final": True}
                )
                qhit = set(pair) == set(fo[:2])
                qk = f"quinella:{pair[0]}-{pair[1]}"
                strategies["model_box3_quinella"].append(
                    {"rid": rid, "jra": jra, "hit": qhit, "snap_odds": 0.0, "final": fodds.get(qk, 0.0) if qhit else 0.0, "has_final": True}
                )

    def report_strategy(name, rows, use_snap=True):
        n = len(rows)
        if n == 0:
            print(f"  {name:<22} n=0")
            return None
        hits = sum(1 for r in rows if r["hit"])
        lo, hi = wilson_ci(hits, n)
        stake = n * 100
        # final odds ROI: hit したのに final が無い行は snap odds で fallback
        ret_final = sum(
            (r["final"] or r["snap_odds"] or 0.0) * 100 for r in rows if r["hit"]
        )
        ret_snap = sum((r["snap_odds"] or 0.0) * 100 for r in rows if r["hit"])
        roi_f = ret_final / stake * 100
        roi_s = ret_snap / stake * 100
        line = (
            f"  {name:<22} n={n:3d} hit={hits:3d} ({hits/n*100:5.1f}%, W95[{lo*100:4.1f},{hi*100:5.1f}])  "
            f"ROI(final)={roi_f:6.1f}%"
        )
        if use_snap:
            line += f"  ROI(snap)={roi_s:6.1f}%"
        print(line)
        return {"n": n, "hits": hits, "hit_pct": hits / n * 100, "roi_final_pct": roi_f, "roi_snap_pct": roi_s, "wilson95": [lo, hi]}

    out["virtual"] = {}
    print("\n[全体]")
    for name, rows in strategies.items():
        out["virtual"][name] = report_strategy(name, rows, use_snap=name not in ("model_box3_wide", "model_box3_quinella"))

    print("\n[JRA のみ]")
    for name, rows in strategies.items():
        report_strategy(name, [r for r in rows if r["jra"]], use_snap=False)
    print("\n[NAR のみ]")
    for name, rows in strategies.items():
        report_strategy(name, [r for r in rows if not r["jra"]], use_snap=False)

    # claude_top1 と model_top1 の同一レース直接対決 (claude index があるレースのみ)
    cl_rids = {r["rid"] for r in strategies["claude_top1_win"]}
    sub_model = [r for r in strategies["model_top1_win"] if r["rid"] in cl_rids]
    sub_mkt = [r for r in strategies["market_top1_win"] if r["rid"] in cl_rids]
    print(f"\n[同一レース比較 (Claude 指数がある {len(cl_rids)} レースのみ)]")
    report_strategy("model_top1_win", sub_model, use_snap=False)
    report_strategy("claude_top1_win", strategies["claude_top1_win"], use_snap=False)
    report_strategy("market_top1_win", sub_mkt, use_snap=False)

    # ════════════════ 4. 較正 (calibration) ════════════════
    print("\n" + "=" * 90)
    print("4. 較正: 予測 1 着確率 bin vs 実勝率")
    print("=" * 90)

    bins = [(0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 0.50), (0.50, 1.01)]

    def calib(probs_actual, label):
        print(f"\n[{label}]  (n={len(probs_actual)} 馬)")
        rows_out = []
        print(f"  {'bin':<12} {'n':>5} {'平均予測':>8} {'実勝率':>8} {'Wilson95%':>16} {'差(実-予)':>9}")
        for lo, hi in bins:
            sub = [(p, a) for p, a in probs_actual if lo <= p < hi]
            if not sub:
                continue
            n = len(sub)
            mean_p = sum(p for p, _ in sub) / n
            wins = sum(a for _, a in sub)
            act = wins / n
            wlo, whi = wilson_ci(wins, n)
            flag = ""
            if mean_p < wlo:
                flag = " ← 悲観 (実勝率が予測より有意に高い)"
            elif mean_p > whi:
                flag = " ← 楽観 (予測が実勝率より有意に高い)"
            print(
                f"  {f'{lo*100:.0f}-{hi*100:.0f}%':<12} {n:>5} {mean_p*100:>7.1f}% {act*100:>7.1f}% "
                f"[{wlo*100:5.1f},{whi*100:5.1f}]% {(act-mean_p)*100:>+8.1f}pt{flag}"
            )
            rows_out.append({"bin": [lo, hi], "n": n, "mean_pred": mean_p, "actual": act, "wilson95": [wlo, whi]})
        return rows_out

    model_pa = []
    llm_pa = []
    for rid, snap, res in joined:
        wt = win_table(snap)
        if not wt:
            continue
        winner = int(res["finish_order"][0])
        for r in wt:
            model_pa.append((r["prob"], 1 if r["n"] == winner else 0))
        llm = snap.get("llm_win_index") or {}
        if llm:
            try:
                idx = {int(k): float(v) for k, v in llm.items()}
            except (ValueError, TypeError):
                continue
            mx = max(idx.values())
            exps = {k: math.exp((v - mx) / T_LLM) for k, v in idx.items()}
            z = sum(exps.values())
            for k, e in exps.items():
                llm_pa.append((e / z, 1 if k == winner else 0))

    out["calibration_model"] = calib(model_pa, "model (bet_tables.win prob, baked blend 込み)")
    out["calibration_llm"] = calib(llm_pa, "Claude 指数 softmax(index/25)")

    if args.json:
        print("\n=== JSON ===")
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
