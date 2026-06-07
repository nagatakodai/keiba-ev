"""ファイルベースのデータアクセス層。

`data/predictions/` のスナップショット、`data/results/` の結果、
`data/cache/auto_watch_analyzed.txt` の自動解析履歴を読む。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PRED_DIR = ROOT / "data" / "predictions"
RESULT_DIR = ROOT / "data" / "results"
AUTO_WATCH_CACHE = ROOT / "data" / "cache" / "auto_watch_analyzed.txt"
AUTO_WATCH_HISTORY = ROOT / "data" / "cache" / "auto_watch_history.jsonl"


def list_predictions(limit: int | None = 100) -> list[dict[str, Any]]:
    """predictions スナップショットのサマリー一覧。saved_at 降順。"""
    if not PRED_DIR.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in PRED_DIR.glob("*.json"):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # 適性指数 top 3 (snapshot は既に total 降順で保存)
        apt = d.get("horse_aptitude") or []
        top_aptitude = [
            {
                "number": a.get("number"),
                "name": a.get("name"),
                "total": a.get("total"),
            }
            for a in apt[:3]
        ]
        items.append(
            {
                "race_id": d.get("race_id") or path.stem,
                "saved_at": d.get("saved_at"),
                "venue_name": d.get("venue_name"),
                "race_class": d.get("race_class"),
                "schedule_index": d.get("schedule_index"),
                "race_number": d.get("race_number"),
                "start_at": d.get("start_at"),
                "close_at": d.get("close_at"),
                "odds_updated_at": d.get("odds_updated_at"),
                "row_count": len(d.get("rows") or []),
                "plan_a_count": len(d.get("plan_a_keys") or []),
                "plan_b_count": len(d.get("plan_b_keys") or []),
                "plan_c_count": len(d.get("plan_c_keys") or []),
                # plan_g_count: Phase 20 で追加された Plan G の point 数。
                # 旧 list_predictions は A/B/C/H1/H2/F のみ exposing しており、
                # Web UI top で G·n バッジが表示されなかった bug を修正。
                "plan_g_count": len(d.get("plan_g_keys") or []),
                "plan_h1_count": len(d.get("plan_h1_keys") or []),
                "plan_h2_count": len(d.get("plan_h2_keys") or []),
                "plan_f_count": len(d.get("plan_f_keys") or []),
                "top_aptitude": top_aptitude,
                "has_evidence": bool(d.get("evidence")),
                "has_result": (RESULT_DIR / f"{path.stem}.json").exists(),
            }
        )
    items.sort(key=lambda x: x.get("saved_at") or "", reverse=True)
    return items if limit is None else items[:limit]


def _safe_race_id(race_id: str) -> str | None:
    """race_id を path-safe な文字に絞って validate する。
    `..` を含むパス traversal、絶対パス、空文字を全て弾く。
    JRA/NAR の race_id は数字 + ハイフン (`-` 正規化形式) のみなので、
    `[A-Za-z0-9_-]+` 以外はリジェクト。"""
    import re
    if not race_id:
        return None
    # 厳格に英数字とハイフン / アンダースコアのみ
    if not re.fullmatch(r"[A-Za-z0-9_-]+", race_id):
        return None
    return race_id


def get_prediction(race_id: str) -> dict[str, Any] | None:
    safe = _safe_race_id(race_id)
    if safe is None:
        return None
    path = PRED_DIR / f"{safe}.json"
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    result_path = RESULT_DIR / f"{safe}.json"
    if result_path.exists():
        try:
            d["result"] = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return d


def list_auto_watch_history(limit: int = 200) -> list[dict[str, Any]]:
    """watch-auto が発火した解析の履歴 (新しい順)。"""
    out: list[dict[str, Any]] = []
    if AUTO_WATCH_HISTORY.exists():
        for line in AUTO_WATCH_HISTORY.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    out.reverse()
    return out[:limit]


def list_analyzed_race_ids() -> list[str]:
    """重複発火防止用キャッシュの内容 (デバッグ用)。"""
    if not AUTO_WATCH_CACHE.exists():
        return []
    return [
        ln.strip()
        for ln in AUTO_WATCH_CACHE.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


def compute_calibration(point_cost: int = 100) -> dict[str, Any]:
    """calibrate.py 相当の集計を JSON で返す。

    フィールド定義 (フロント参照用):
      race_count: predictions ↔ results が join 成立した全レース数。
      tiers[].rows: tier 内の prediction 行の総数 (全レース合算)。
      tiers[].prob_sum: tier 内の prob 合計 (= 期待 hit 数)。
      tiers[].hits: 実 hit 数 (3 連単 finish と一致した buy 目の数 / 全レース合算)。
      tiers[].ratio: hits / prob_sum。1.0 で整合、>1 で過小予測、<1 で過大予測。

      plans[].races: race_count と同じ (= 全 join レース)。Plan が 0 点しか出さなかった
                     レースも含む。「Plan が買い目を出したレース数」は participated_races。
      plans[].participated_races: その Plan の buy 目が 1 点以上あったレース数。
                                   plans[].hit_rate = hits / participated_races の方が
                                   "Plan が出した時の的中率" として直感的。
      plans[].hits: その Plan の買い目のいずれかが finish_order と一致したレース数。
                    1 レース内で複数当たっても 1 (Plan は exclusive な選択肢のため)。
      plans[].total_points: 全レース合算の Plan 内 buy 目数 (= 賭けた点数の総和)。
      plans[].point_cost: 1 点あたりの単価 (¥)。
                          リクエスト時の point_cost (default 100) をそのまま使う。
                          実運用予算ベースで欲しければ plans[].assumed_budget_slot /
                          (avg points/race) で計算可。
      plans[].assumed_budget_slot: 実運用予算上の各 Plan の枠 (¥)。
                                    A/B/C は ¥8,000 (EV 枠を 1 つ選ぶ前提)、
                                    H1/H2 は ¥2,000 (当て枠を 1 つ選ぶ前提)。
      plans[].stake: total_points × point_cost。
      plans[].payout: hit 時の trifecta_payout 合算 (100 円あたり払戻単位)。
      plans[].roi: payout / stake。
      plans[].hit_rate: hits / participated_races (Plan が出した時の的中率)。

      plans[].hit_rate_ci_low / hit_rate_ci_high: hit_rate の Wilson 95% 信頼区間。
          サンプル小 (participated_races < 30) では区間が広いので、UI 側で「サンプル
          不足」表示と組み合わせて使う想定。
      plans[].roi_ci_low / roi_ci_high: ROI の bootstrap 95% 信頼区間
          (1000 回 resample)。同様にサンプル不足時は広い区間が返る。

      sample_warning: race_count < 30 で true。フロント側で「数字は参考程度」表示の
          トリガーに使える。CLAUDE.md 「保守化の禁則」(最低 30 レース) と整合。

      last_updated_at: 集計に含めた results の最新 recorded_at (ISO8601 文字列)。
                       「いつまでのデータか」を UI で出すための ETag 的フィールド。

      races[].has_evidence: その race の prediction で LLM 評価 + 補強根拠抽出が
                            走った場合 true (snapshot に `evidence` フィールドあり)。
                            calibration 結果を「LLM 込みパイプライン本来の性能」と
                            「確率モデルのみ」で分離評価するための discriminator。
      races[].result_source: 結果の取得経路。"winningOddsIds" / "results" / "auto" /
                              "backfill-YYYY-MM-DD" など。手動 record や backfill 由来
                              を pipeline 由来と区別したい時に使う。
      races[].saved_at: prediction snapshot が保存された ISO8601 時刻。デプロイ前後で
                        パイプライン挙動が変わった場合のセグメント分析に使う。

      evidence_race_count: race_count のうち has_evidence=true の数。
      non_evidence_race_count: race_count のうち has_evidence=false の数。
    """
    from collections import defaultdict
    import random
    from math import sqrt

    pairs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    last_updated_at: str | None = None
    # **集計 cutoff** (2026-05-29 ユーザ指示): Plan A/B → bundle 集約と Claude 選定の
    # 大幅な spec 変更があったので、Claude 回収率 / 的中率を **今日 (JST 2026-05-29) 以降の
    # snapshot だけ** で計算しなおす。古い snapshot は dashboard / 履歴ページに表示しない。
    # 後で延ばしたければ CALIBRATION_CUTOFF_ISO_JST を更新するだけ。
    CALIBRATION_CUTOFF_ISO_JST = "2026-05-29T00:00:00"
    # **3連単的中モード (実弾投票束) の計測開始日** (2026-06-06 ユーザ指示: 昨日=6/5 から)。
    # それ以前の snapshot にも recommended_bundle_t が乗っていることがあるが、実弾運用前の
    # 試行なので trifecta_bundle 集計 (ダッシュボードの的中率/回収率/収支/チャート) には
    # 入れない。per-race の表示 (履歴ページの badge 等) は従来通り残す。
    TRIFECTA_CUTOFF_ISO_JST = "2026-06-05T00:00:00"
    if PRED_DIR.exists():
        for pred_path in sorted(PRED_DIR.glob("*.json")):
            race_id = pred_path.stem
            result_path = RESULT_DIR / f"{race_id}.json"
            if not result_path.exists():
                continue
            try:
                pred = json.loads(pred_path.read_text(encoding="utf-8"))
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            # cutoff より古い snapshot は除外 (saved_at は JST naive ISO8601 文字列なので
            # そのまま辞書順比較で OK)
            saved_at = pred.get("saved_at") or ""
            if saved_at < CALIBRATION_CUTOFF_ISO_JST:
                continue
            # **結果未取得レースは計測に入れない** (2026-06-06 ユーザ指示)。
            # 結果ファイルが無いレースは上の result_path.exists() で join 段階から除外済。
            # ここではさらに「結果ファイルはあるが finish_order が欠落/不完全 (3着まで
            # 揃わない)」な placeholder/壊れ result も除外する — 空の finish のまま通すと
            # 3連単的中モード等の計測で「参加・不的中」に誤計上されるため。
            finish = result.get("finish_order") or []
            if len(finish) < 3 or any(not isinstance(x, int) or x <= 0 for x in finish[:3]):
                continue
            pairs.append((race_id, pred, result))
            r_ts = result.get("recorded_at")
            if isinstance(r_ts, str) and (last_updated_at is None or r_ts > last_updated_at):
                last_updated_at = r_ts

    tier_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"prob_sum": 0.0, "hits": 0, "rows": 0}
    )
    races: list[dict[str, Any]] = []

    # **新スキーマ (2026-05-29 後半)**: Plan A/B/C/F/G/H1/H2 は全廃。3連単 を「他券種と同じ
    # EV 解析結果」として bet_tables に含めて表示。集計対象は **2 つの bundle のみ**:
    #   - recommended_bundle      : 回収優先 (実弾で買う、joint Kelly EV 最適)
    #   - recommended_bundle_hit  : 的中優先 (おまけ計測、prob 降順 pool で Kelly)
    # 旧 snapshot との互換は (a) plan_*_keys 集計は完全に削除 (b) recommended_bundle_hit は
    # 古い snapshot で欠落 → claude_bundle_hit aggregate は 0 集計になる (本日以降に蓄積)。

    for race_id, pred, result in pairs:
        finish_tuple = tuple(result["finish_order"])
        payout = int(result.get("trifecta_payout") or 0)
        winning_tier: str | None = None
        for r in pred.get("rows", []):
            tier = r.get("tier", "?")
            tier_stats[tier]["prob_sum"] += r["prob"]
            tier_stats[tier]["rows"] += 1
            if tuple(r["key"]) == finish_tuple:
                tier_stats[tier]["hits"] += 1
                winning_tier = tier

        # 束 (回収優先 / 的中優先) の的中を bet-type-aware で判定。
        # ワイド/馬連/馬単/3連複/単複 も含めた全 bet type を考慮 (portfolio._bet_hits)。
        from src.portfolio import _bet_hits
        a3, b3, c3 = (list(finish_tuple) + [0, 0, 0])[:3]

        # **最終オッズ** (2026-05-29~): result["final_odds"] = `{leg_id: final_odds}` (`leg_id`
        # は `"<bet_type>:<key-with-->"` 形式, llm.leg_id と一致)。result fetch 時に保存される。
        # 古い result では空 → snapshot odds (予想時点) に fallback して旧挙動を維持する。
        final_odds = result.get("final_odds") or {}

        def _leg_id_for(leg: dict) -> str:
            bt = leg.get("bet_type", "")
            key = leg.get("key") or []
            return f"{bt}:{'-'.join(str(k) for k in key)}"

        def _bundle_stats(bundle: dict | None) -> dict:
            """bundle dict → {legs, hit_legs, stake, payout(予想), payout_final(最終),
            participated, hit} の集計。

            payout (snapshot odds 基準) は従来通り保持。
            payout_final は **実最終オッズ × stake** で計算 (final_odds から lookup)。
            final_odds 不在脚は snapshot odds に fallback (= payout と同じ値)。
            """
            legs = (bundle or {}).get("legs") or []
            hit_legs = [
                leg for leg in legs
                if _bet_hits(leg.get("bet_type", ""), tuple(leg.get("key", [])), a3, b3, c3)
            ]
            payout_snapshot = sum(int(leg.get("payout_if_hit", 0)) for leg in hit_legs)
            # 実払戻 (最終オッズ × stake) を計算。final_odds 不在脚は snapshot odds で補完。
            payout_final = 0
            for leg in hit_legs:
                stake = int(leg.get("stake", 0))
                fo = final_odds.get(_leg_id_for(leg))
                if fo is None or fo <= 0:
                    payout_final += int(leg.get("payout_if_hit", 0))
                else:
                    payout_final += int(round(stake * float(fo)))
            return {
                "legs": legs,
                "hit_legs": hit_legs,
                "stake": sum(int(leg.get("stake", 0)) for leg in legs),
                "payout": payout_snapshot,            # 予想オッズ基準
                "payout_final": payout_final,         # 最終オッズ基準 (実払戻に近い)
                "participated": bool(legs),
                "hit": bool(hit_legs),
            }

        b_yield = _bundle_stats(pred.get("recommended_bundle"))
        # 3連単的中モード(market 無視・Claude 指数フォーメーション)。
        # 回収優先 bundle と完全分離して集計し、ダッシュボードで並べて見せる。
        # 古い snapshot は recommended_bundle_t 欠落 → participated=False で分母外。
        # **Claude 指数ゲート** (2026-06-07 ユーザ指示): rank_source != "claude" の束
        # (model フォールバック) は auto_watch / oddspark_bet / ipat_bet が実弾投票を
        # 弾くため、legs が立っていても実際には賭けていない。計測上も「見送り」として
        # 扱う (participated=False / stake=0 / hit=False) — 賭けていないレースを
        # 「参加・不的中」に誤計上しない。
        bundle_t = pred.get("recommended_bundle_t") or {}
        if bundle_t.get("rank_source") != "claude":
            bundle_t = {}
        b_t = _bundle_stats(bundle_t)

        races.append(
            {
                "race_id": race_id,
                "venue": pred.get("venue_name") or "",
                "finish": list(finish_tuple),
                "winning_tier": winning_tier,
                "payout": payout,
                # EV束 (recommended_bundle, モデルのみの参考値。2026-06-06 以降は投票しない)。
                # bundle_payout = 予想オッズ基準 (snapshot)、bundle_payout_final = 最終オッズ基準。
                "bundle_hit": b_yield["hit"],
                "bundle_hit_bet_types": sorted({leg["bet_type"] for leg in b_yield["hit_legs"]}),
                "bundle_participated": b_yield["participated"],
                "bundle_stake": b_yield["stake"],
                "bundle_payout": b_yield["payout"],
                "bundle_payout_final": b_yield["payout_final"],
                # 3連単的中モード bundle (**実弾投票束**。2026-06-06 以降 3連単的中モード固定)。
                "trifecta_bundle_hit": b_t["hit"],
                "trifecta_bundle_hit_bet_types": sorted({leg["bet_type"] for leg in b_t["hit_legs"]}),
                "trifecta_bundle_participated": b_t["participated"],
                "trifecta_bundle_stake": b_t["stake"],
                "trifecta_bundle_payout": b_t["payout"],
                "trifecta_bundle_payout_final": b_t["payout_final"],
                # 3連単的中モードの計測対象か (saved_at >= TRIFECTA_CUTOFF)。
                # False の race は trifecta_bundle 集計とダッシュボードのチャートから除外。
                "trifecta_measured": (pred.get("saved_at") or "") >= TRIFECTA_CUTOFF_ISO_JST,
                # 最終オッズが取れたかの discriminator (frontend で「予想/最終 切替表示」用)
                "has_final_odds": bool(final_odds),
                # LLM 評価有無の discriminator
                "has_evidence": bool(pred.get("evidence")),
                "saved_at": pred.get("saved_at"),
                "result_source": result.get("source", "unknown"),
            }
        )

    tiers_out: list[dict[str, Any]] = []
    for tier in ("honsen", "chuana", "oana", "minus"):
        if tier not in tier_stats:
            continue
        s = tier_stats[tier]
        ratio = s["hits"] / s["prob_sum"] if s["prob_sum"] > 0 else 0.0
        tiers_out.append(
            {
                "tier": tier,
                "rows": s["rows"],
                "prob_sum": s["prob_sum"],
                "hits": s["hits"],
                "ratio": ratio,
            }
        )

    evidence_count = sum(1 for r in races if r.get("has_evidence"))

    def _bundle_agg(
        races_: list[dict], part_field: str, hit_field: str,
        stake_field: str, payout_field: str, payout_final_field: str,
    ) -> dict:
        """bundle (3連単束 実弾 / EV束参考) の集計。見送り (participated=false) は除外。

        payout (予想) と payout_final (最終オッズ) の両方で ROI を出す。
        """
        part = [r for r in races_ if r.get(part_field)]
        per_race = [
            (int(r.get(stake_field, 0)), int(r.get(payout_field, 0)))
            for r in part
        ]
        per_race_final = [
            (int(r.get(stake_field, 0)), int(r.get(payout_final_field, 0)))
            for r in part
        ]
        hits = sum(1 for r in part if r.get(hit_field))
        n = len(part)
        stake_sum = sum(s for s, _ in per_race)
        payout_sum = sum(p for _, p in per_race)
        payout_final_sum = sum(p for _, p in per_race_final)
        hit_rate = hits / n if n else 0.0
        roi = (payout_sum / stake_sum) if stake_sum else 0.0
        roi_final = (payout_final_sum / stake_sum) if stake_sum else 0.0
        hr_low, hr_high = _wilson_ci(hits, n)
        roi_low, roi_high = _bootstrap_roi_ci(per_race)
        roi_final_low, roi_final_high = _bootstrap_roi_ci(per_race_final)
        return {
            "races": len(races_),
            "participated_races": n,
            "skipped_races": len(races_) - n,
            "hits": hits,
            "hit_rate": hit_rate,
            "hit_rate_ci_low": hr_low,
            "hit_rate_ci_high": hr_high,
            "stake": stake_sum,
            # 予想オッズ基準
            "payout": payout_sum,
            "roi": roi,
            "roi_ci_low": roi_low,
            "roi_ci_high": roi_high,
            # 最終オッズ基準 (実払戻に近い)
            "payout_final": payout_final_sum,
            "roi_final": roi_final,
            "roi_final_ci_low": roi_final_low,
            "roi_final_ci_high": roi_final_high,
        }

    # EV束 (recommended_bundle, モデルのみの参考値。2026-06-06 以降は投票しない)。
    claude_bundle = _bundle_agg(
        races, "bundle_participated", "bundle_hit",
        "bundle_stake", "bundle_payout", "bundle_payout_final",
    )
    # 3連単的中モードの集計 (**実弾投票束**, 2026-06-06〜固定)。EV束と同形。
    # 計測対象は TRIFECTA_CUTOFF 以降のみ (それ以前は races/skipped の分母からも除外)。
    trifecta_bundle = _bundle_agg(
        [r for r in races if r.get("trifecta_measured")],
        "trifecta_bundle_participated", "trifecta_bundle_hit",
        "trifecta_bundle_stake", "trifecta_bundle_payout", "trifecta_bundle_payout_final",
    )

    return {
        "race_count": len(pairs),
        "point_cost": point_cost,
        "last_updated_at": last_updated_at,
        "sample_warning": len(pairs) < 30,
        "evidence_race_count": evidence_count,
        "non_evidence_race_count": len(pairs) - evidence_count,
        "tiers": tiers_out,
        "plans": [],
        "claude_bundle": claude_bundle,
        "trifecta_bundle": trifecta_bundle,
        # 3連単的中モードの計測開始日 (frontend の注記表示用)
        "trifecta_cutoff": TRIFECTA_CUTOFF_ISO_JST,
        "races": races,
    }


def _wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二項分布の Wilson 95% 信頼区間。
    n=0 は (0, 0)、n が小さいほど区間は広い。"""
    from math import sqrt
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _bootstrap_roi_ci(
    per_race: list[tuple[int, int]],
    n_iter: int = 1000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float]:
    """ROI の bootstrap 95% 信頼区間。
    per_race: 各レースの (stake, payout) tuple。
    レース単位で resample → ROI 計算 → 2.5/97.5 percentile を返す。
    seed 固定で結果が決定的になる (フロントの再 fetch で値が踊らない)。"""
    import random as _rnd
    n = len(per_race)
    if n == 0:
        return (0.0, 0.0)
    total_stake = sum(s for s, _ in per_race)
    if total_stake == 0:
        return (0.0, 0.0)
    rng = _rnd.Random(seed)
    rois: list[float] = []
    for _ in range(n_iter):
        idx = [rng.randrange(n) for _ in range(n)]
        s_sum = sum(per_race[i][0] for i in idx)
        p_sum = sum(per_race[i][1] for i in idx)
        rois.append(p_sum / s_sum if s_sum > 0 else 0.0)
    rois.sort()
    lo_idx = int(n_iter * (alpha / 2))
    hi_idx = int(n_iter * (1 - alpha / 2)) - 1
    return (rois[lo_idx], rois[hi_idx])
