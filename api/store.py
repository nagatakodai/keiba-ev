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


def get_prediction(race_id: str) -> dict[str, Any] | None:
    path = PRED_DIR / f"{race_id}.json"
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    result_path = RESULT_DIR / f"{race_id}.json"
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
            pairs.append((race_id, pred, result))
            r_ts = result.get("recorded_at")
            if isinstance(r_ts, str) and (last_updated_at is None or r_ts > last_updated_at):
                last_updated_at = r_ts

    tier_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"prob_sum": 0.0, "hits": 0, "rows": 0}
    )
    plan_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "hits": 0,
            "payouts": 0,
            "races": 0,
            "participated_races": 0,
            "total_points": 0,
            # bootstrap 用に per-race の (stake, payout) を保持
            "per_race": [],
        }
    )
    races: list[dict[str, Any]] = []

    # Plan A/B/C/G は EV 枠 ¥8,000、H1/H2 は当て枠 ¥2,000 (analyze.py の hit_budget_ratio=0.2)。
    # Plan F は union 採用なので ¥10,000 全額枠。
    BUDGET_SLOT = {
        "Plan A": 8000, "Plan B": 8000, "Plan C": 8000, "Plan G": 8000,
        "Plan H1": 2000, "Plan H2": 2000,
        "Plan F": 10000,
    }
    PLAN_KEY_FIELDS = (
        ("Plan A", "plan_a_keys"),
        ("Plan B", "plan_b_keys"),
        ("Plan C", "plan_c_keys"),
        ("Plan G", "plan_g_keys"),
        ("Plan H1", "plan_h1_keys"),
        ("Plan H2", "plan_h2_keys"),
        ("Plan F", "plan_f_keys"),
    )

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

        race_plan_hits: dict[str, bool] = {}
        for plan_name, field_name in PLAN_KEY_FIELDS:
            key_list_raw = pred.get(field_name)
            # 古いスナップショットにキー不在 → そのレースはこの plan の集計対象外
            # (空リスト [] = 「Plan は出したが picks 0」とは区別する)
            if key_list_raw is None:
                continue
            key_list = key_list_raw
            plan_stats[plan_name]["races"] += 1
            plan_stats[plan_name]["total_points"] += len(key_list)
            if key_list:
                plan_stats[plan_name]["participated_races"] += 1
            hit = tuple(finish_tuple) in {tuple(k) for k in key_list}
            race_plan_hits[plan_name] = hit
            if hit:
                plan_stats[plan_name]["hits"] += 1
                if payout > 0:
                    plan_stats[plan_name]["payouts"] += payout
            # per-race contribution: stake = points × point_cost、payout = hit 時のみ
            race_stake = len(key_list) * point_cost
            race_payout = payout if hit else 0
            plan_stats[plan_name]["per_race"].append((race_stake, race_payout))

        races.append(
            {
                "race_id": race_id,
                "venue": pred.get("venue_name") or "",
                "finish": list(finish_tuple),
                "winning_tier": winning_tier,
                "payout": payout,
                "plan_a_hit": race_plan_hits.get("Plan A", False),
                "plan_b_hit": race_plan_hits.get("Plan B", False),
                "plan_c_hit": race_plan_hits.get("Plan C", False),
                "plan_g_hit": race_plan_hits.get("Plan G", False),
                "plan_h1_hit": race_plan_hits.get("Plan H1", False),
                "plan_h2_hit": race_plan_hits.get("Plan H2", False),
                "plan_f_hit": race_plan_hits.get("Plan F", False),
                # LLM 評価有無の discriminator (LLM 込みデータと無しを混ぜないため)
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

    plans_out: list[dict[str, Any]] = []
    for name, _ in PLAN_KEY_FIELDS:
        s = plan_stats[name]
        if s["races"] == 0:
            continue
        stake = s["total_points"] * point_cost
        roi = (s["payouts"] / stake) if stake > 0 else 0.0
        participated = s["participated_races"]
        hit_rate = s["hits"] / participated if participated else 0.0
        hr_low, hr_high = _wilson_ci(s["hits"], participated)
        roi_low, roi_high = _bootstrap_roi_ci(s["per_race"])
        plans_out.append(
            {
                "plan": name,
                "races": s["races"],
                "participated_races": participated,
                "hits": s["hits"],
                "hit_rate": hit_rate,
                "hit_rate_ci_low": hr_low,
                "hit_rate_ci_high": hr_high,
                "total_points": s["total_points"],
                "point_cost": point_cost,
                "assumed_budget_slot": BUDGET_SLOT.get(name, 0),
                "stake": stake,
                "payout": s["payouts"],
                "roi": roi,
                "roi_ci_low": roi_low,
                "roi_ci_high": roi_high,
            }
        )

    evidence_count = sum(1 for r in races if r.get("has_evidence"))
    return {
        "race_count": len(pairs),
        "point_cost": point_cost,
        "last_updated_at": last_updated_at,
        "sample_warning": len(pairs) < 30,
        "evidence_race_count": evidence_count,
        "non_evidence_race_count": len(pairs) - evidence_count,
        "tiers": tiers_out,
        "plans": plans_out,
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
