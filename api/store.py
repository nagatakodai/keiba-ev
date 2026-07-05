"""ファイルベースのデータアクセス層。

`data/predictions/` のスナップショット、`data/results/` の結果、
`data/cache/auto_watch_analyzed.txt` の自動解析履歴を読む。
"""
from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PRED_DIR = ROOT / "data" / "predictions"
RESULT_DIR = ROOT / "data" / "results"
AUTO_WATCH_CACHE = ROOT / "data" / "cache" / "auto_watch_analyzed.txt"
AUTO_WATCH_HISTORY = ROOT / "data" / "cache" / "auto_watch_history.jsonl"
TIMELINE_DIR = ROOT / "data" / "cache" / "odds_timeline"
# 今日の勝負レース スキャン結果 (src/shobu.py が <date>.json を書く)。
SHOBU_DIR = ROOT / "data" / "cache" / "shobu"

# **市場非依存 Claude 指数の開始時刻** (commit 022b003, 2026-06-21 19:04 JST)。
# これ以前は score プロンプトに単勝オッズ列があり Claude 指数が市場由来だったので、
# shobu 仮想収支 (BOX/戦略くらべ) の計測から除外する (ユーザ指示 2026-06-30:
# 「Claude指数を市場から導出していた頃のデータは計測したくない」)。判定は llm_scored_at→saved_at。
MARKET_INDEPENDENT_CUTOFF_ISO_JST = "2026-06-21T19:04:27"


def _scored_at(snap: dict[str, Any]) -> str:
    """snapshot の Claude 指数採点時刻 (llm_scored_at 優先・無ければ saved_at)。市場由来 cutoff 判定用。"""
    return snap.get("llm_scored_at") or snap.get("saved_at") or ""


def shobu_today_jst() -> str:
    """当日 (JST) を YYYYMMDD で返す (shobu の out path / 既定 date 用)。"""
    import datetime
    from zoneinfo import ZoneInfo
    return datetime.datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d")


def get_shobu_result(date: str | None = None) -> dict[str, Any] | None:
    """勝負レース スキャン結果 (data/cache/shobu/<date>.json) を読む。無ければ None。"""
    import re
    d = date or shobu_today_jst()
    if not re.fullmatch(r"\d{8}", d or ""):
        return None
    p = SHOBU_DIR / f"{d}.json"
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # 各レースに仮想購入の的中券種ラベルを付与 (配信時に snapshot+result から都度計算)。
    return attach_hit_labels(doc)


def index_version_of(snap: dict[str, Any]) -> str | None:
    """snapshot の Claude 指数 方針バージョン (β=市場由来 / v1=補強3件 / v2=無制限 / v3=仮指数アンカー・現行)。

    明示の `index_version` (2026-06-30〜 保存) があればそれを返す。無い旧 snapshot は **採点日時で推定**:
    Claude 指数が無ければ None、市場由来 cutoff (2026-06-21 19:04) 以前は **β** (score プロンプトに
    単勝オッズ列があり市場由来だった頃・ユーザ指示 2026-06-30「β版として残して表示」)、それ以降
    INDEX_V2_SINCE (2026-06-28) 未満は v1、INDEX_V3_SINCE (2026-07-01 15:13 = 仮指数アンカー移行
    dbff5b2) 未満は v2、以降は v3。

    「Claude 指数あり」は index_compare の行に **claude_index が実際に入っているか** で判定する
    (market-only refresh の snapshot は market_index のみの行を持つため、行の存在だけ見ると
    指数ゼロのレースが β/v1/v2 に誤分類され version 母数が過大になる)。
    """
    from src.llm import INDEX_V2_SINCE, INDEX_V3_SINCE
    v = snap.get("index_version")
    if v:
        # 仮指数アンカー移行 (07-01 15:13) 〜 INDEX_VERSION="v3" 反映までの間に保存された
        # snapshot は "v2" が誤刻印されている → 採点日時で v3 に矯正 (真の v2 は cutoff 前のみ)。
        if v == "v2" and _scored_at(snap) >= INDEX_V3_SINCE:
            return "v3"
        return v
    has_index = any(r.get("claude_index") is not None
                    for r in (snap.get("index_compare") or [])) or bool(snap.get("llm_win_index"))
    if not has_index:
        return None
    scored = _scored_at(snap)
    if scored < MARKET_INDEPENDENT_CUTOFF_ISO_JST:
        return "β"
    if scored < INDEX_V2_SINCE:
        return "v1"
    return "v3" if scored >= INDEX_V3_SINCE else "v2"


def list_predictions(limit: int | None = 100) -> list[dict[str, Any]]:
    """predictions スナップショットのサマリー一覧。saved_at 降順。"""
    if not PRED_DIR.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in PRED_DIR.glob("*.json"):
        if path.name.endswith(".llm.json"):
            continue   # score 段の指数キャッシュ (ghost 行・race_id 重複の原因) は除外
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
        # ダッシュボード仮想購入 (BOX+戦略くらべ) の的中券種ラベル (ユーザ指示 2026-07-04)。
        # EV束/3連単束 (実弾) の的中とは無関係。結果未確定/判定不能は None。
        result_path = RESULT_DIR / f"{path.stem}.json"
        hit_strategies: list[dict[str, Any]] | None = None
        if result_path.exists():
            try:
                hit_strategies = hit_bet_labels(
                    d, json.loads(result_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        items.append(
            {
                "race_id": d.get("race_id") or path.stem,
                "saved_at": d.get("saved_at"),
                # "score"=Claude 指数出力時の暫定 / "bet"=締切直前の確定。旧 snapshot は欠落→bet 相当。
                "stage": d.get("stage") or "bet",
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
                # 補強根拠 (evidence) 方針バージョン (v1=3件上限 / v2=無制限)。指数なしは null。
                "index_version": index_version_of(d),
                "has_result": result_path.exists(),
                "hit_strategies": hit_strategies,
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


def netkeiba_rid_from_internal(race_id: str) -> str | None:
    """内部 race_id "<cup_id>-<schedule_index>-<race_number>" → netkeiba 12桁 rid。

    cup_id 長で NAR(10桁 YYYY+場+MMDD)/JRA(8桁 YYYY+場+回) を判別し parse._split_race_id の
    逆変換を行う。netkeiba rid は snapshot に保存していない (旧 snapshot 含む) が race_id から
    完全復元できるので、これでオッズ再取得の経路 (scrape_*/analyze) を組み直せる。
    復元不能 (形式不正 / 12桁にならない) なら None。
    """
    parts = (race_id or "").split("-")
    if len(parts) != 3:
        return None
    cup_id, si, rn = parts
    if not (cup_id.isdigit() and si.isdigit() and rn.isdigit()):
        return None
    if len(cup_id) == 10:      # NAR: YYYY + 場(2) + MMDD
        rid = f"{cup_id}{int(rn):02d}"
    elif len(cup_id) == 8:     # JRA: YYYY + 場(2) + 開催回(2)
        rid = f"{cup_id}{int(si):02d}{int(rn):02d}"
    else:
        return None
    return rid if (len(rid) == 12 and rid.isdigit()) else None


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
    # オッズ更新ボタン用: 経路 (netkeiba 経路は欠落=None) と再取得可否 (rid 復元可) を surface。
    d.setdefault("odds_source", None)
    d["can_refresh"] = netkeiba_rid_from_internal(d.get("race_id") or safe) is not None
    # 補強根拠 (evidence) 方針バージョン (旧 snapshot は採点日時で v1/v2 推定)。
    d["index_version"] = index_version_of(d)
    return d


def get_timeline(race_id: str) -> dict[str, Any] | None:
    """`data/cache/odds_timeline/<race_id>.jsonl` のオッズ時系列 + 確定結果。

    UI チャート用の軽量レスポンス: 各行の odds は **win/place のみ** に絞る
    (3連単グリッドは最大 N(N-1)(N-2)=数千組で payload が巨大になるため)。
    券種ごとの組数は `depth` メタデータとして残す (poll の捕捉カバレッジ確認用)。
    結果 (`data/results/<race_id>.json`) があれば finish_order + final_odds を埋め込む
    (final_odds は束の脚 or 払戻組のみで小さい)。timeline ファイルが無ければ None。
    """
    safe = _safe_race_id(race_id)
    if safe is None:
        return None
    path = TIMELINE_DIR / f"{safe}.jsonl"
    if not path.exists():
        return None
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue   # 壊れた行は skip (append 中のクラッシュ等)
        odds = d.get("odds") or {}
        rows.append(
            {
                "stage": d.get("stage"),
                "captured_at": d.get("captured_at"),
                "close_at": d.get("close_at") or 0,
                "start_at": d.get("start_at") or 0,
                "n_horses": d.get("n_horses") or 0,
                "odds": {bt: odds[bt] for bt in ("win", "place") if odds.get(bt)},
                "depth": {bt: len(v) for bt, v in odds.items()},
                "source": d.get("source"),
            }
        )
    result_out: dict[str, Any] | None = None
    result_path = RESULT_DIR / f"{safe}.json"
    if result_path.exists():
        try:
            r = json.loads(result_path.read_text(encoding="utf-8"))
            result_out = {
                "finish_order": r.get("finish_order") or [],
                "final_odds": r.get("final_odds") or {},
            }
        except (json.JSONDecodeError, OSError):
            pass
    return {"race_id": safe, "rows": rows, "result": result_out}


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
    # **EV束 (実弾既定束) の計測開始時刻** (2026-06-10 d2afa47: 投票束の既定が EV束になった時点)。
    # それ以前の EV束は β=0 事故 (de-vig no-op / 一様確率で最長オッズ購入 / full Kelly) 込みの
    # 別物の戦略なので、ev_bundle 系列 (ダッシュボード) には混ぜない。全期間の参考集計は
    # claude_bundle (旧名のまま互換維持) に残る。
    EV_CUTOFF_ISO_JST = "2026-06-10T18:21:00"
    if PRED_DIR.exists():
        for pred_path in sorted(PRED_DIR.glob("*.json")):
            if pred_path.name.endswith(".llm.json"):
                continue   # 指数キャッシュは計測対象外 (result join 不成立で偶然 skip されていたが明示ガード)
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
        # 出走頭数 (複勝の頭数ルール: 7頭以下=2着まで・4頭以下=発売なし を hit 判定に適用)。
        # snapshot の n_runners (権威値, 2026-06-11〜) を最優先。無ければ
        # win_probs_model → bet_tables.win → horse_aptitude の順で推定 (win テーブルは
        # odds≤0 馬を除外するため最大数頭の過小があり得る — 旧 snapshot のみの妥協)。
        n_runners = pred.get("n_runners") or None
        if not n_runners:
            for src_field in (pred.get("win_probs_model"),
                              (pred.get("bet_tables") or {}).get("win"),
                              pred.get("horse_aptitude")):
                if src_field:
                    n_runners = len(src_field)
                    break

        # **最終オッズ** (2026-05-29~): result["final_odds"] = `{leg_id: final_odds}` (`leg_id`
        # は `"<bet_type>:<key-with-->"` 形式, llm.leg_id と一致)。result fetch 時に保存される。
        # 古い result では空 → snapshot odds (予想時点) に fallback して旧挙動を維持する。
        final_odds = result.get("final_odds") or {}

        def _leg_id_for(leg: dict) -> str:
            bt = leg.get("bet_type", "")
            key = leg.get("key") or []
            # 順不同券種 (馬連/ワイド/3連複) は昇順正規化してから join (2026-06-12)。
            # result["final_odds"] の leg_id は parse._parse_payout_table 等が昇順で
            # 保存する規約 (bundle_calibration_report._final_odds_key と同じ)。現データの
            # leg key は全件昇順だが、unsorted な key が将来入っても lookup が崩れない。
            if bt in ("quinella", "wide", "trio"):
                key = sorted(key)
            return f"{bt}:{'-'.join(str(k) for k in key)}"

        # 同着 (dead heat) 対応 (2026-06-11 bughunt 第4R): netkeiba-html result の
        # final_odds は**払戻があった組のみ**の payout テーブル。finish_order は同着の
        # 片側しか持てないため、leg_id がテーブルに載っていれば finish 不一致でも実払戻
        # あり = 的中。keibago/jra/auto の final_odds は束の全脚のオッズ snapshot
        # (的中と無関係に載る) なのでこの経路では使わない。
        _payout_table = final_odds if result.get("source") == "netkeiba-html" else {}

        def _leg_hit(leg: dict) -> bool:
            if _bet_hits(leg.get("bet_type", ""), tuple(leg.get("key", [])),
                         a3, b3, c3, n_runners):
                return True
            return _leg_id_for(leg) in _payout_table

        def _bundle_stats(bundle: dict | None) -> dict:
            """bundle dict → {legs, hit_legs, stake, payout(予想), payout_final(最終),
            participated, hit} の集計。

            payout (snapshot odds 基準) は従来通り保持。
            payout_final は **実最終オッズ × stake** で計算 (final_odds から lookup)。
            final_odds 不在脚は snapshot odds に fallback (= payout と同じ値)。
            """
            legs = (bundle or {}).get("legs") or []
            hit_legs = [leg for leg in legs if _leg_hit(leg)]
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

        # **backfill 束の除外** (2026-06-12, bundle_calibration_report と同 semantics):
        # scripts/backfill_bundle.py が後付けした束 (backfilled=true) は実際には賭けて
        # いない paper なので、EV束系列 (ev_bundle / claude_bundle) では「見送り」として
        # 扱う (participated=False / stake=0)。race 行自体は残し、bundle_backfilled
        # フラグで UI 側がグレーアウトできるようにする。
        # stage="score" の暫定プレビュー (Claude 指数出力時に履歴へ早出ししたもの) は
        # bet 段で上書きされるはず。上書きされず score のまま残るのは **bet が発火しなかった
        # = 賭けなかった**レースなので、backfill と同様「見送り」扱い (participated=False)。
        # 履歴 (list_predictions/get_prediction) には出すが、ROI 計測の分母には入れない。
        # stage 欠落の旧 snapshot は bet 相当として通す。
        is_score_preview = (pred.get("stage") == "score")
        bundle_ev = pred.get("recommended_bundle") or {}
        bundle_backfilled = bool(bundle_ev.get("backfilled"))
        b_yield = _bundle_stats({} if (bundle_backfilled or is_score_preview) else bundle_ev)
        # 3連単的中モード(market 無視・Claude 指数フォーメーション)。
        # 回収優先 bundle と完全分離して集計し、ダッシュボードで並べて見せる。
        # 古い snapshot は recommended_bundle_t 欠落 → participated=False で分母外。
        # **Claude 指数ゲート** (2026-06-07 ユーザ指示): rank_source != "claude" の束
        # (model フォールバック) は auto_watch / oddspark_bet / ipat_bet が実弾投票を
        # 弾くため、legs が立っていても実際には賭けていない。計測上も「見送り」として
        # 扱う (participated=False / stake=0 / hit=False) — 賭けていないレースを
        # 「参加・不的中」に誤計上しない。
        bundle_t = pred.get("recommended_bundle_t") or {}
        if is_score_preview or bundle_t.get("rank_source") != "claude":
            bundle_t = {}
        b_t = _bundle_stats(bundle_t)

        races.append(
            {
                "race_id": race_id,
                "venue": pred.get("venue_name") or "",
                "finish": list(finish_tuple),
                "winning_tier": winning_tier,
                "payout": payout,
                # EV束 (recommended_bundle)。2026-06-10〜 実弾既定束 (KEIBA_BET_BUNDLE=ev)。
                # bundle_payout = 予想オッズ基準 (snapshot)、bundle_payout_final = 最終オッズ基準。
                "bundle_hit": b_yield["hit"],
                "bundle_hit_bet_types": sorted({leg["bet_type"] for leg in b_yield["hit_legs"]}),
                "bundle_participated": b_yield["participated"],
                "bundle_stake": b_yield["stake"],
                "bundle_payout": b_yield["payout"],
                "bundle_payout_final": b_yield["payout_final"],
                # backfill された paper 束 (実際には賭けていない)。集計からは除外済、
                # UI のグレーアウト表示用フラグ。
                "bundle_backfilled": bundle_backfilled,
                # stage="score" の暫定プレビュー (bet 未発火で score 止まり = 賭けていない)。
                # 集計は見送り扱い、UI は「暫定」表示できる。bet 段で上書きされれば False。
                "stage": pred.get("stage") or "bet",
                "stage_preview": is_score_preview,
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
                # EV束 (実弾既定束) の計測対象か (saved_at >= EV_CUTOFF = 修正版 EV束の稼働開始)。
                # False の race は ev_bundle 集計とダッシュボードの EV束系列から除外。
                "ev_measured": (pred.get("saved_at") or "") >= EV_CUTOFF_ISO_JST,
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

    # EV束の全期間参考集計 (旧名 claude_bundle のまま互換維持。β=0 事故時代を含むので
    # ダッシュボードの実弾系列には使わない — そちらは ev_bundle)。
    claude_bundle = _bundle_agg(
        races, "bundle_participated", "bundle_hit",
        "bundle_stake", "bundle_payout", "bundle_payout_final",
    )
    # EV束 (**実弾既定束**, 2026-06-10〜 KEIBA_BET_BUNDLE=ev)。計測対象は EV_CUTOFF 以降のみ
    # (= 修正版 EV束: de-vig 修正 / β=0.78 / ドリフトシェード / px_o≤2.0 / ½Kelly)。
    ev_bundle = _bundle_agg(
        [r for r in races if r.get("ev_measured")],
        "bundle_participated", "bundle_hit",
        "bundle_stake", "bundle_payout", "bundle_payout_final",
    )
    # 3連単的中モードの集計 (2026-06-06〜10 は実弾固定束、以降は KEIBA_BET_BUNDLE=trifecta
    # 選択時の実弾束)。EV束と同形。計測対象は TRIFECTA_CUTOFF 以降のみ。
    trifecta_measured = [r for r in races if r.get("trifecta_measured")]
    trifecta_bundle = _bundle_agg(
        trifecta_measured,
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
        "ev_bundle": ev_bundle,
        "trifecta_bundle": trifecta_bundle,
        # 各系列の計測開始日 (frontend の注記表示用)
        "trifecta_cutoff": TRIFECTA_CUTOFF_ISO_JST,
        "ev_cutoff": EV_CUTOFF_ISO_JST,
        "races": races,
    }


def _claude_index_by_number(snap: dict[str, Any]) -> dict[int, float]:
    """snapshot から 馬番→Claude 指数 を抽出 (shobu._claude_edge と同じ源)。

    index_compare (list, 各馬の claude_index/market_index 比較・権威) を優先、無ければ
    llm_win_index (dict {馬番: 指数})。Claude 指数が無いレースは空 dict。
    market_index は不要 (上位5頭は Claude 指数のみで決まる)。
    """
    out: dict[int, float] = {}
    ic = snap.get("index_compare")
    if isinstance(ic, list) and ic:
        for r in ic:
            ci = r.get("claude_index")
            num = r.get("number")
            if ci is None or num is None:
                continue
            try:
                out[int(num)] = float(ci)
            except (TypeError, ValueError):
                continue
        if out:
            return out
    for k, ci in (snap.get("llm_win_index") or {}).items():
        try:
            out[int(k)] = float(ci)
        except (TypeError, ValueError):
            continue
    return out


def _shobu_box_size(n_runners: int | None, base: int = 5) -> int:
    """3連単 BOX に使う上位頭数 (フィールドの大きさに応じて縮める)。

    基本は上位 base 頭 (=5) だが、頭数が少ないと 5頭 BOX がフィールドの大半を覆って
    「上位5頭に1-2-3着が収まる」が当たり前になり screen の意味が薄れる。そこで
    **最低3頭は BOX 外に残す** ことにし `box = min(base, n − 3)` とする。
    ユーザ指示 (2026-06-21): **7頭立ては4頭BOX** (= 7 − 3)。trifecta は最低3頭なので 3 で floor。
      n ≥ 8 → 5頭 / n = 7 → 4頭 / n = 6 → 3頭 / n ≤ 5 → 3頭。
    """
    if not n_runners or n_runners <= 0:
        return base
    return max(3, min(base, n_runners - 3))


def _shobu_eval_races(recommended_only: bool) -> dict[str, dict[str, Any]]:
    """shobu スキャン結果 (data/cache/shobu/*.json) を race_id で集約して返す共通ヘルパ。

    再スキャンの重複は generated_at の後勝ち。`recommended_only=True` は推奨 (勝負レース) のみ、
    False は推奨に限らず shobu が評価した全レース (= 当日スキャンの母集団)。
    BOX 収支 (`_shobu_box_pnl`) と 戦略くらべ (`_strategies_pnl`) が同じ母集団定義を共有する。
    値には `_generated_at` を付与 (date 推定/後勝ち判定用)。
    """
    by_race: dict[str, dict[str, Any]] = {}
    if SHOBU_DIR.exists():
        for p in sorted(SHOBU_DIR.glob("*.json")):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            gen = doc.get("generated_at") or ""
            for race in doc.get("races") or []:
                rid = race.get("race_id")
                if not rid:
                    continue
                prev = by_race.get(rid)
                if prev is None or gen >= (prev.get("_generated_at") or ""):
                    by_race[rid] = {**race, "_generated_at": gen}
    # recommended フィルタは dedup (generated_at 後勝ち) の **後** に最新コピーで判定する。
    # 先にフィルタすると、同一 race_id が複数 file にあり最新スキャンで recommended=False に
    # 落ちた場合に古い recommended=True のコピーが母集団に残留する (latent、2026-07-04 修正)。
    if recommended_only:
        by_race = {rid: r for rid, r in by_race.items() if r.get("recommended")}
    return by_race


def _box_race_pnl(
    snap: dict[str, Any],
    result: dict[str, Any],
    *,
    point_cost: int,
    box_size: int,
    meta: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """1 レースの **Claude 指数上位 N 頭の 3連単 BOX** 仮想収支を計算する共通ヘルパ。

    compute_shobu_pnl (勝負レース=recommended) と compute_indexed_pnl (shobu 評価レース全体) で
    共有する。返り値 `(detail | None, reason)`。reason は "ok" / "no_index" / "no_result":
      - "no_index": Claude 指数が 3 頭未満 (BOX 不能)。
      - "no_result": 着順が 3 着まで確定していない (result file 欠落も呼び側が {} を渡せば該当)。
    `meta` は detail に載せる race_id/date/venue/race_no/race_type/shobu_score/matched/n_runners。
    """
    from itertools import permutations

    idx = _claude_index_by_number(snap)
    if len(idx) < 3:
        return None, "no_index"
    n_runners = snap.get("n_runners") or meta.get("n_runners") or len(idx)
    box = _shobu_box_size(n_runners, base=box_size)
    # 同点指数は馬番昇順で明示タイブレーク (従来の実効挙動と同一。index_compare の
    # 行順依存を排し、行構築順の将来変更で過去計測の対象馬が入れ替わらないようにする)。
    top = [num for num, _ci in sorted(idx.items(), key=lambda kv: (-kv[1], kv[0]))][:box]
    top_set = set(top)
    finish = [x for x in (result.get("finish_order") or [])[:3]
              if isinstance(x, int) and x > 0]
    if len(finish) < 3:
        return None, "no_result"
    n_points = len(list(permutations(top, 3)))     # = P(len(top), 3)
    stake = n_points * point_cost
    # 同着対応 (2026-07-04): 的中3連単組 (同着時は複数) のうち BOX が覆う組を数える。
    # 同着なしは従来と同値 (的中組 = finish そのもの 1 組)。BOX が同着の両組を覆えば両方の
    # 払戻を得る。finish 組は trifecta_payout、他の同着組は final_odds から payout を引く。
    ranks = _finish_ranks(result)
    covered = [c for c in _winning_trifectas(ranks) if all(x in top_set for x in c)]
    hit = bool(covered)
    tri = int(result.get("trifecta_payout") or 0)
    fo = result.get("final_odds") or {}
    payout = 0
    for c in covered:
        if list(c) == finish[:3] and tri:
            payout += int(round(tri * point_cost / 100.0))
        else:
            try:
                o = float(fo.get(f"trifecta:{c[0]}-{c[1]}-{c[2]}") or 0)
            except (TypeError, ValueError):
                o = 0.0
            payout += int(round(o * point_cost))
    detail = {
        "race_id": meta.get("race_id"),
        "date": meta.get("date") or "",
        "venue": meta.get("venue") or "",
        "race_no": meta.get("race_no"),
        "race_type": meta.get("race_type"),
        "shobu_score": meta.get("shobu_score"),
        "matched": meta.get("matched") or [],
        "n_runners": n_runners,
        "box": len(top),
        "top_horses": top,
        "finish": finish,
        "n_points": n_points,
        "stake": stake,
        "hit": hit,
        "payout": payout,
        "trifecta_payout": tri,
        "saved_at": snap.get("saved_at"),
    }
    return detail, "ok"


def _venue_filter(by_race: dict[str, dict[str, Any]],
                  venue: str | None) -> dict[str, dict[str, Any]]:
    """競馬場フィルタ (ユーザ指示 2026-07-05: ダッシュボードを 地方/中央/ばんえい の別ページに分離)。

    venue="jra" = 中央 (race_type == "jra") / "banei" = 帯広ばんえい (race_type == "banei"、
    別競技なので地方平地と混ぜない — ev.segment_of_rd と同じ3区分) / "nar" = 地方平地
    (それ以外 = jra でも banei でもない。race_type 欠落の旧 doc も地方に落とす) /
    None = 従来どおり全レース (後方互換)。母集団 dict の段階で絞るので
    recommended_total / skipped_* / races_detail もすべて venue スコープになる。
    """
    if venue is None:
        return by_race

    def _of(r: dict[str, Any]) -> str:
        rt = r.get("race_type")
        return rt if rt in ("jra", "banei") else "nar"

    return {rid: r for rid, r in by_race.items() if _of(r) == venue}


def _shobu_box_pnl(
    point_cost: int = 100, box_size: int = 5, *, recommended_only: bool = True,
    version: str | None = None, venue: str | None = None,
) -> dict[str, Any]:
    """**shobu スキャンが評価したレース** の Claude 指数上位N頭3連単BOX 仮想収支 (共通コア)。

    各レースで Claude 指数 **上位 box_size 頭** (既定 5) の **3連単 BOX** を買ったと仮定し、
    実際の 1・2・3 着がその上位N頭に全て収まれば的中として trifecta 配当で収支を集計する。
    `recommended_only=True` (compute_shobu_pnl): 勝負レース(推奨)のみ。
    `recommended_only=False` (compute_indexed_pnl): 推奨に限らず **shobu が評価した全レース**
    (= 当日スキャンの母集団。data/predictions 全体ではないので betting pipeline の過去スコアは
    混ざらない — ユーザ指摘 2026-06-28「全レースがこんなに多いはずがない・ほとんど推奨のはず」)。

    - 対象: data/cache/shobu/<date>.json の (recommended_only なら recommended の) レースのうち、
      Claude 指数があり (上位N頭を決められる) 結果が確定しているもの。
    - 上位N頭 = snapshot の Claude 指数 (index_compare / llm_win_index) 降順。N は
      `_shobu_box_size(出走頭数)` (≥8頭=5 / 7頭=4 / 少頭数は最低3頭を場外に残す)。
    - 3連単 BOX 点数 = P(N, 3) = 60点(5頭) / 24点(4頭)。stake = 点数 × point_cost。
    - payout = 的中時の trifecta_payout を point_cost(¥100単位)へスケール。
    返り値は hits/hit_rate/stake/payout/roi + CI + per-race detail。指数なし=skipped_no_index、
    結果未確定=skipped_no_result (分母外)。recommended_total = 集約レース数 (= recommended_only なら
    勝負レース総数 / False なら shobu 評価レース総数)。
    `venue` ("nar"/"jra") で 地方/中央 に母集団を分離 (ユーザ指示 2026-07-05、None=全レース)。
    """
    # shobu 評価レースを race_id で集約 (再スキャンの重複は generated_at 後勝ち)。
    by_race = _venue_filter(_shobu_eval_races(recommended_only), venue)

    races_detail: list[dict[str, Any]] = []
    per_race: list[tuple[int, int]] = []
    hits = 0
    stake_sum = 0
    payout_sum = 0
    skipped_no_index = 0
    skipped_no_result = 0
    pop = 0   # version 指定時の母集団 (= そのバージョンのレース総数)
    last_updated_at: str | None = None

    for rid, race in by_race.items():
        safe = _safe_race_id(rid)
        if safe is None:
            continue
        snap_path = PRED_DIR / f"{safe}.json"
        if not snap_path.exists():
            if version is None:
                skipped_no_index += 1   # version 指定時はバージョン不明なので集計外
            continue
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            if version is None:
                skipped_no_index += 1
            continue
        # 補強根拠バージョン (β/v1/v2) フィルタ (ユーザ指示 2026-06-30: 計測をバージョン毎に分離)。
        if version is not None and index_version_of(snap) != version:
            continue
        pop += 1
        result_path = RESULT_DIR / f"{safe}.json"
        result: dict[str, Any] = {}
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                result = {}
        meta = {
            "race_id": rid,
            "date": (race.get("_generated_at") or "")[:10],
            "venue": race.get("venue") or "",
            "race_no": race.get("race_no"),
            "race_type": race.get("race_type"),
            "shobu_score": race.get("shobu_score"),
            "matched": race.get("matched") or [],
            "n_runners": race.get("n_runners"),
        }
        detail, reason = _box_race_pnl(
            snap, result, point_cost=point_cost, box_size=box_size, meta=meta)
        if reason == "no_index":
            skipped_no_index += 1
            continue
        if reason != "ok" or detail is None:
            skipped_no_result += 1   # result file 欠落 / 着順未確定
            continue

        hits += 1 if detail["hit"] else 0
        stake_sum += detail["stake"]
        payout_sum += detail["payout"]
        per_race.append((detail["stake"], detail["payout"]))
        r_ts = result.get("recorded_at")
        if isinstance(r_ts, str) and (last_updated_at is None or r_ts > last_updated_at):
            last_updated_at = r_ts
        races_detail.append(detail)

    n = len(per_race)
    hit_rate = hits / n if n else 0.0
    roi = (payout_sum / stake_sum) if stake_sum else 0.0
    hr_low, hr_high = _wilson_ci(hits, n)
    roi_low, roi_high = _bootstrap_roi_ci(per_race)
    races_detail.sort(key=lambda r: (r.get("saved_at") or r.get("date") or ""), reverse=True)
    return {
        "point_cost": point_cost,
        "box_size": box_size,
        "races": n,                         # 集計成立 (指数+結果あり) レース数
        "hits": hits,
        "hit_rate": hit_rate,
        "hit_rate_ci_low": hr_low,
        "hit_rate_ci_high": hr_high,
        "stake": stake_sum,
        "payout": payout_sum,
        "roi": roi,
        "roi_ci_low": roi_low,
        "roi_ci_high": roi_high,
        # 集約レース総数 (version 指定時はそのバージョンのレース数 / 未指定は全集約数)。
        "recommended_total": pop if version is not None else len(by_race),
        "version": version,
        "venue": venue,
        "skipped_no_index": skipped_no_index,
        "skipped_no_result": skipped_no_result,
        "last_updated_at": last_updated_at,
        "sample_warning": n < 30,
        "races_detail": races_detail,
    }


def compute_shobu_pnl(point_cost: int = 100, box_size: int = 5,
                      version: str | None = None,
                      venue: str | None = None) -> dict[str, Any]:
    """勝負レース (recommended) 専用の仮想収支 (ダッシュボード hero。ユーザ指示 2026-06-21)。"""
    return _shobu_box_pnl(point_cost, box_size, recommended_only=True, version=version,
                          venue=venue)


def compute_indexed_pnl(point_cost: int = 100, box_size: int = 5,
                        version: str | None = None,
                        venue: str | None = None) -> dict[str, Any]:
    """**shobu が評価した全レース** (recommended に限らない) の仮想収支 (別カード併記)。

    ユーザ指示 (2026-06-28): 「Claude 指数が全ての馬についていて結果があればダッシュボードに反映」
    → ただし母集団は **当日スキャンが評価したレース** (data/predictions 全体ではない)。
    ユーザ指摘で betting pipeline の過去スコア (~06-12〜) が混ざって 153 件に膨れていたのを修正し、
    shobu 評価レース (recommended + 非recommended) に scope。これで「ほとんど推奨」(推奨カードの
    proper superset) になる。指数条件は推奨カードと同じ (BOX 可能=指数3頭以上) で揃える。
    `version` ("v1"/"v2"/"v3"/"β") を渡すと Claude 指数バージョン毎に分離 (ユーザ指示 2026-06-30)。
    `venue` ("nar"/"jra") で 地方/中央 に分離 (ユーザ指示 2026-07-05)。
    """
    return _shobu_box_pnl(point_cost, box_size, recommended_only=False, version=version,
                          venue=venue)


# ============================================================
# Claude 指数の単純戦略くらべ (ユーザ指示 2026-06-30)
#   - win1       : 指数1位の単勝
#   - place23    : 指数2,3位の複勝 (各 point_cost)
#   - quinella12 : 指数1-2位の馬連
#   - winplace   : 単複 (1位単勝 + 2,3位複勝) = win1 + place23
# 払戻は result["final_odds"] の win:/place:/quinella: (= ×100 オッズ。例 win:7=19.2 → ¥1920)。
# 複勝は頭数ルール (_place_cutoff)、馬連は上位2着で判定。final_odds は all-or-nothing
# (実測 489件は全券種揃い / 98件は全欠落) なので no_odds は「final_odds 無し + どこかの脚が的中」の
# レース全体スキップで扱い、全戦略の母集団を揃える (的中脚のみオッズを要求・外れ脚は¥0)。
# ============================================================

def _place_cutoff(n_runners: int | None) -> int:
    """複勝が払い戻される着順数 (頭数ルール)。

    4頭以下=発売なし(0) / 5-7頭=2着まで(2) / 8頭以上=3着まで(3)。
    None (頭数不明) は従来の top-3 とみなす (portfolio._bet_hits と同じ規約)。
    """
    if n_runners is None:
        return 3
    if n_runners <= 4:
        return 0
    if n_runners <= 7:
        return 2
    return 3


# 順不同券種 (組番を昇順正規化して照合する)。馬単/3連単は着順そのまま。
_UNORDERED_BETS = {"quinella", "wide", "trio"}


def _snap_combo_odds(snap: dict[str, Any], bet_type: str) -> dict[tuple[int, ...], float]:
    """snapshot の bet_tables から **組番(tuple)→snapshot 時点オッズ** を取り出す (全券種)。

    「オッズ ≤1.1 なら買わない」等の **着順非依存 (買う前) のフィルタ判定** に使う
    (result の final_odds は in-money 組のオッズしか無く着順依存になるため不適)。
    **実態の注意 (2026-07-04 実測)**: ①shobu 経路の snapshot は全て stage="score" で、
    オッズは **スキャン時 / 自動再score 時 (推奨レースは締切2-7分前, 06-30〜) のもの** —
    「締切直前の最終オッズ」ではない (旧 docstring は誤り。stale 判定は実測で fired 脚の
    realized>1.1 が 41%)。②複勝/ワイドは全 writer がレンジ **下限** のみ保存するため
    フィルタは下限判定 = 保守的に過剰発動する (place1 の母集団を ~44% 削る)。いずれも
    系列の定義安定を優先して仕様として維持 (閾値 sweep は非単調で改善の裏付け無し、
    CLAUDE.md 2026-07-04 の記録参照)。順不同券種 (馬連/ワイド/3連複) は組番を昇順正規化。
    単勝/複勝は全馬・組合せ券種は経路により疎 (netkeiba 経路は pair 系が空) なので、
    組番が表に無ければフィルタ no-op (= オッズ不明なら買う)。
    """
    out: dict[tuple[int, ...], float] = {}
    for row in ((snap.get("bet_tables") or {}).get(bet_type) or []):
        key = row.get("key") or []
        odds = row.get("odds")
        if not key or not odds:
            continue
        try:
            nums = [int(x) for x in key]
            k = tuple(sorted(nums)) if bet_type in _UNORDERED_BETS else tuple(nums)
            out[k] = float(odds)
        except (TypeError, ValueError):
            continue
    return out


# **全券種** で最終オッズが ≤ この値なら買わない (ユーザ指示 2026-06-30: 旨味の無い大本命を除外)。
_MIN_ODDS = 1.1


def _finish_ranks(result: dict[str, Any]) -> dict[int, int]:
    """result → 着順 1-3 の全馬 {馬番: 着順}。**同着 (dead heat) 対応** (2026-07-04)。

    `finish_positions` (writer が着順表の全行から構築・同着は複数馬が同じ着順) を優先。
    整合チェック: 各着順 r は「r より上位の馬の数 + 1」に一致するはず (例 [1,2,2]・[1,1,3]・
    [1,2,3,3] は valid、[1,3,3]・[2,3,3] は invalid)。invalid / 欠落の旧 result は
    finish_order から一意着順を構成する (従来と同値)。
    """
    pos = result.get("finish_positions") or {}
    out: dict[int, int] = {}
    for k, v in pos.items():
        try:
            n, r = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if n > 0 and 1 <= r <= 3:
            out[n] = r
    vals = sorted(out.values())
    if len(vals) >= 3 and all(r == 1 + sum(1 for x in vals if x < r) for r in set(vals)):
        return out
    return {x: i + 1 for i, x in enumerate((result.get("finish_order") or [])[:3])
            if isinstance(x, int) and x > 0}


def _winning_trifectas(ranks: dict[int, int]) -> list[tuple[int, int, int]]:
    """的中3連単組の列挙 (同着対応)。同着なしなら着順どおりの 1 組。

    的中組 = 3頭の rank が非減少かつ top3 パターン (上位3ポジション) と一致する順列。
    例: 3着同着 [1,2,3,3] → (1着,2着,3着a) と (1着,2着,3着b) の 2 組 /
    2着同着 [1,2,2] → 1着→2着a→2着b と 1着→2着b→2着a の 2 組。
    """
    from itertools import permutations
    inm = [n for n, r in ranks.items() if r <= 3]
    if len(inm) < 3:
        return []
    pat = sorted(ranks[n] for n in inm)[:3]
    out: list[tuple[int, int, int]] = []
    for c in permutations(sorted(inm), 3):
        rs = [ranks[x] for x in c]
        if rs[0] <= rs[1] <= rs[2] and sorted(rs) == pat:
            out.append(c)
    return out
# 戦略メタ (表示順・ラベル・代表券種)。races_detail / strategies のキー順もこれに従う。
# 複勝は 1/2/3 位を分けて計測 (ユーザ指示 2026-06-30)。
# (単複 winplace は 2026-06-30 ユーザ指示で全表示から撤去)。
STRATEGY_DEFS = [
    # 並び順はユーザ指示 (2026-07-05): 単勝1,2,3 → 複勝1,2,3 → 馬連1-2,1-3 → ワイド1-2,1-3 →
    # その他。win2/win3/quinella13 は券種比較グリッドのために追加 (同日)。
    ("win1", "単勝 (指数1位)", "win"),
    ("win2", "単勝 (指数2位)", "win"),
    ("win3", "単勝 (指数3位)", "win"),
    ("place1", "複勝 (指数1位)", "place"),
    ("place2", "複勝 (指数2位)", "place"),
    ("place3", "複勝 (指数3位)", "place"),
    ("quinella12", "馬連 (指数1-2位)", "quinella"),
    ("quinella13", "馬連 (指数1-3位)", "quinella"),
    ("wide12", "ワイド (指数1-2位)", "wide"),
    ("wide13", "ワイド (指数1-3位)", "wide"),
    ("exacta12", "馬単 (指数1→2位)", "exacta"),
    ("trifecta123", "3連単 (指数1→2→3)", "trifecta"),
    ("trio123", "3連複 (指数1-2-3)", "trio"),
    ("trio1234box", "3連複BOX (指数1-2-3-4)", "trio"),
    ("wide123box", "ワイドBOX (指数1-2-3)", "wide"),
]


def _strategy_race_legs(
    snap: dict[str, Any],
    result: dict[str, Any],
    *,
    point_cost: int,
    meta: dict[str, Any],
    ranking: list[int] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """1 レースの Claude 指数戦略 (STRATEGY_DEFS の各戦略) の脚を計算する共通ヘルパ。

    返り値 `(detail | None, reason)`。reason は "ok"/"no_index"/"no_result"/"no_odds":
      - "no_index": ランキングが 3 頭未満 (1・2・3 位を決められない)。
      - "no_result": 着順が 3 着まで未確定。
      - "no_odds": **的中した脚** の払戻オッズ (final_odds の win:/place:/quinella:) が欠落
        (keiba.go.jp fallback 等で final_odds 未保存) → 払戻を評価できないので分母外。
        外れ脚は払戻 0 なのでオッズ欠落でも問題ない (的中脚のみオッズを要求する)。
    detail["per"][戦略key] = {stake, payout, hit, bets, hits}。

    `ranking` を渡すと Claude 指数の代わりにその馬番順 (降順・1位が先頭) を「上位」とみなす。
    市場人気ベースライン (市場指数順で同じ買い方をした場合の ROI) を、オッズ/≤1.1フィルタ/
    的中判定を Claude 版と**完全に同一のロジック**で計算するために使う (市場一致シグナルの参考値)。
    未指定 (既定) は従来どおり Claude 指数から順位を決める (挙動不変)。
    """
    if ranking is not None:
        ranked = [int(n) for n in ranking]
    else:
        idx = _claude_index_by_number(snap)
        # 同点指数は馬番昇順で明示タイブレーク (_box_race_pnl と同じ理由)。
        ranked = [num for num, _ci in sorted(idx.items(), key=lambda kv: (-kv[1], kv[0]))]
    if len(ranked) < 3:
        return None, "no_index"
    top1, top2, top3 = ranked[0], ranked[1], ranked[2]

    finish = [x for x in (result.get("finish_order") or [])[:3]
              if isinstance(x, int) and x > 0]
    if len(finish) < 3:
        return None, "no_result"

    n_runners = snap.get("n_runners") or meta.get("n_runners") or len(ranked)
    cutoff = _place_cutoff(n_runners)
    # 同着対応 (2026-07-04): 的中判定は finish_order (一意3頭) でなく着順 rank {馬番: 着順}
    # で行う。同着が無ければ ranks == {finish[i]: i+1} で従来と完全同値。
    ranks = _finish_ranks(result)
    inmoney = sorted(ranks.values())          # 例 [1,2,3] / 3着同着 [1,2,3,3] / 2着同着 [1,2,2]
    top2_pat = inmoney[:2]                    # 馬連/馬単の的中 rank パターン
    top3_pat = inmoney[:3]                    # 3連複/3連単の的中 rank パターン

    def _r(n: int) -> int:
        return ranks.get(n, 99)

    placed_set = {n for n, r in ranks.items() if r <= cutoff} if cutoff else set()

    fo = result.get("final_odds") or {}
    # 全券種の最終オッズ (締切直前スナップショット, 組番→オッズ)。買う前の ≤1.1 フィルタに使う。
    snap_odds = {bt: _snap_combo_odds(snap, bt)
                 for bt in ("win", "place", "quinella", "wide", "exacta", "trio", "trifecta")}

    def _odds(key: str) -> float | None:
        v = fo.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    missing_odds = False

    def _leg(bet_type: str, key_nums: list[int], hit: bool, odds_key: str) -> dict[str, Any]:
        """1 脚 = {bet_type,key,hit,bet,stake,payout}。

        **全券種で最終オッズ ≤ _MIN_ODDS(1.1) なら買わない** (bet=False・ユーザ指示 2026-06-30)。
        判定はスナップショットの bet_tables (買う前の組番別オッズ)。組番が表に無ければ買う
        (オッズ不明)。`bet=False` 脚は stake/payout=0・集計対象外。**実際に買った的中脚**の
        払戻 (result final_odds) 欠落のみ missing_odds を立てる (呼び元が no_odds でレース除外)。"""
        nonlocal missing_odds
        norm = (tuple(sorted(key_nums)) if bet_type in _UNORDERED_BETS else tuple(key_nums))
        pre = snap_odds.get(bet_type, {}).get(norm)
        bet = not (pre is not None and pre <= _MIN_ODDS)
        pay = 0
        if hit and bet:
            o = _odds(odds_key)
            if o is None:
                missing_odds = True
            else:
                pay = int(round(o * point_cost))
        return {"bet_type": bet_type, "key": key_nums, "hit": hit, "bet": bet,
                "stake": point_cost if bet else 0, "payout": pay}

    # --- 的中判定 (rank ベース・同着対応) -------------------------------------
    # 同着なしでは従来判定と完全同値。同着時は JRA/NAR の払戻ルールに一致:
    #   馬連/馬単 = ペアの rank が top2_pat と一致 (2着同着の 2-2 は不的中・1着同着の 1-1 は的中)
    #   3連複/3連単 = 3頭の rank が top3_pat と一致 (3着同着 [1,3,3] は不的中)
    #   ワイド = 両馬 rank≤3、ただし 3着同着同士 (3,3) は不的中
    def _quinella_hit(a: int, b: int) -> bool:
        return sorted((_r(a), _r(b))) == top2_pat

    def _exacta_hit(a: int, b: int) -> bool:
        return _r(a) <= _r(b) and sorted((_r(a), _r(b))) == top2_pat

    def _wide_hit(a: int, b: int) -> bool:
        ra, rb = _r(a), _r(b)
        return ra <= 3 and rb <= 3 and not (ra == 3 and rb == 3 and len(inmoney) > 3)

    def _trio_hit(a: int, b: int, c: int) -> bool:
        return sorted((_r(a), _r(b), _r(c))) == top3_pat

    def _trifecta_hit(a: int, b: int, c: int) -> bool:
        return _r(a) <= _r(b) <= _r(c) and _trio_hit(a, b, c)

    # 単勝 (1位) — 頭数に関係なく常に発売。最終オッズ ≤1.1 は買わない (全券種共通フィルタ)。
    win_leg = _leg("win", [top1], _r(top1) == 1, f"win:{top1}")
    # 単勝 (2位 / 3位) — 券種比較グリッド用 (ユーザ指示 2026-07-05)。
    win_leg_2 = _leg("win", [top2], _r(top2) == 1, f"win:{top2}")
    win_leg_3 = _leg("win", [top3], _r(top3) == 1, f"win:{top3}")
    # 複勝 (1位 / 2位 / 3位 を分けて計測) — 複勝が発売される頭数のときだけ。
    place_leg_1 = (_leg("place", [top1], top1 in placed_set, f"place:{top1}")
                   if cutoff > 0 else None)
    place_leg_2 = (_leg("place", [top2], top2 in placed_set, f"place:{top2}")
                   if cutoff > 0 else None)
    place_leg_3 = (_leg("place", [top3], top3 in placed_set, f"place:{top3}")
                   if cutoff > 0 else None)
    # 馬連 (1-2位)。key は昇順 (final_odds の規約に合わせる)。
    qa, qb = sorted((top1, top2))
    quin_leg = _leg("quinella", [qa, qb], _quinella_hit(top1, top2), f"quinella:{qa}-{qb}")
    # 馬連 (1-3位) — 券種比較グリッド用 (ユーザ指示 2026-07-05)。判定は quinella12 と同型。
    qc, qd = sorted((top1, top3))
    quin13_leg = _leg("quinella", [qc, qd], _quinella_hit(top1, top3), f"quinella:{qc}-{qd}")
    # 馬単 (1→2位)。key は着順そのまま。
    exacta_leg = _leg("exacta", [top1, top2],
                      _exacta_hit(top1, top2), f"exacta:{top1}-{top2}")

    # 3連単 (1→2→3)。final_odds の trifecta key は着順そのまま。
    trifecta_leg = _leg("trifecta", [top1, top2, top3], _trifecta_hit(top1, top2, top3),
                        f"trifecta:{top1}-{top2}-{top3}")
    # 3連複 (1-2-3)。key は昇順。
    t3 = sorted((top1, top2, top3))
    trio_leg = _leg("trio", t3, _trio_hit(top1, top2, top3), f"trio:{t3[0]}-{t3[1]}-{t3[2]}")
    # 3連複 BOX (1-2-3-4) — C(4,3)=4 点。
    box_legs: list[dict[str, Any]] = []
    if len(ranked) >= 4:
        top4 = ranked[:4]
        for combo in combinations(top4, 3):
            cs = sorted(combo)
            box_legs.append(_leg("trio", cs, _trio_hit(*combo),
                                 f"trio:{cs[0]}-{cs[1]}-{cs[2]}"))
    # ワイド (1-2位)。key 昇順 (final_odds 規約)。
    wa, wb = sorted((top1, top2))
    wide12_leg = _leg("wide", [wa, wb], _wide_hit(top1, top2), f"wide:{wa}-{wb}")
    # ワイド (1-3位) — 指数1位×3位 (ユーザ指示 2026-07-02)。判定は wide12 と同型。
    wc, wd = sorted((top1, top3))
    wide13_leg = _leg("wide", [wc, wd], _wide_hit(top1, top3), f"wide:{wc}-{wd}")
    # ワイドBOX (1-2-3) — C(3,2)=3 点 (複数同時的中あり)。
    wide_box_legs: list[dict[str, Any]] = []
    for a, b in combinations((top1, top2, top3), 2):
        pa, pb = sorted((a, b))
        wide_box_legs.append(_leg("wide", [pa, pb], _wide_hit(a, b), f"wide:{pa}-{pb}"))

    if missing_odds:
        return None, "no_odds"     # 実際に買った的中脚の払戻不明 → 評価不能

    def _agg(legs: list[dict[str, Any]]) -> dict[str, Any]:
        bl = [l for l in legs if l["bet"]]   # 実際に買った脚のみ集計
        return {
            "stake": sum(l["stake"] for l in bl),
            "payout": sum(l["payout"] for l in bl),
            "bets": len(bl),
            "hits": sum(1 for l in bl if l["hit"]),
            "hit": any(l["payout"] > 0 for l in bl),
        }

    per = {
        "win1": _agg([win_leg]),
        "win2": _agg([win_leg_2]),
        "win3": _agg([win_leg_3]),
        "place1": _agg([place_leg_1] if place_leg_1 else []),
        "place2": _agg([place_leg_2] if place_leg_2 else []),
        "place3": _agg([place_leg_3] if place_leg_3 else []),
        "quinella12": _agg([quin_leg]),
        "quinella13": _agg([quin13_leg]),
        "wide12": _agg([wide12_leg]),
        "wide13": _agg([wide13_leg]),
        "exacta12": _agg([exacta_leg]),
        "trifecta123": _agg([trifecta_leg]),
        "trio123": _agg([trio_leg]),
        "trio1234box": _agg(box_legs),
        "wide123box": _agg(wide_box_legs),
    }
    detail = {
        "race_id": meta.get("race_id"),
        "date": meta.get("date") or "",
        "venue": meta.get("venue") or "",
        "race_no": meta.get("race_no"),
        "race_type": meta.get("race_type"),
        "shobu_score": meta.get("shobu_score"),
        "n_runners": n_runners,
        "place_cutoff": cutoff,
        "top1": top1,
        "top2": top2,
        "top3": top3,
        "finish": finish,
        "per": per,
        "saved_at": snap.get("saved_at"),
    }
    return detail, "ok"


def _strategies_pnl(point_cost: int = 100, *, recommended_only: bool = True,
                    version: str | None = None,
                    venue: str | None = None) -> dict[str, Any]:
    """**shobu 評価レース** の Claude 指数 単純戦略くらべ 仮想収支 (共通コア)。

    各レースで win1 (1位単勝) / place1,2,3 (1/2/3位複勝) / quinella12 (1-2位馬連) /
    wide12,13 (1-2位/1-3位ワイド) / exacta12 (1→2位馬単) / trifecta123 / trio123 /
    trio1234box / wide123box を仮定し、
    実着順と final_odds の払戻で **戦略ごとに** 収支を集計する。母集団 (`_shobu_eval_races`) は BOX 収支と
    共有: `recommended_only=True` = 勝負レース(推奨)のみ / False = 推奨に限らず shobu が評価した全レース。

    返り値 `strategies` は STRATEGY_DEFS 順の各戦略 {key,label,bet_type, races,races_hit,bets,hits,
    hit_rate(+CI),stake,payout,net,roi(+CI)}。**hit_rate の母数はレース数** (= races_hit/races・
    ユーザ指示 2026-06-30)。`bets`/`hits` は脚単位 (trio1234box=4脚・wide123box=3脚/レース) で stake 算出用。
    `races` は **実際に 1 脚以上買ったレース数** (フィルタ後): 単勝/複勝は最終オッズ ≤1.1、単複は合成オッズ
    <1 (1位複勝オッズ/2<1) で買い見送り、複勝は ≤4頭で発売なし → いずれも races から外れる。
    指数<3頭=skipped_no_index、結果未確定=skipped_no_result、的中脚オッズ欠落=skipped_no_odds。
    `venue` ("nar"/"jra") で 地方/中央 に母集団を分離 (ユーザ指示 2026-07-05、None=全レース)。
    """
    by_race = _venue_filter(_shobu_eval_races(recommended_only), venue)

    # 戦略ごとの集計器。races_hit = レース単位の的中数 (hit_rate の分子)。
    acc = {key: {"races": 0, "races_hit": 0, "bets": 0, "hits": 0, "stake": 0,
                 "payout": 0, "per_race": []} for key, _label, _bt in STRATEGY_DEFS}
    races_detail: list[dict[str, Any]] = []
    races_n = 0
    pop = 0   # version 指定時の母集団 (= そのバージョンのレース総数)
    skipped_no_index = skipped_no_result = skipped_no_odds = 0
    last_updated_at: str | None = None

    for rid, race in by_race.items():
        safe = _safe_race_id(rid)
        if safe is None:
            continue
        snap_path = PRED_DIR / f"{safe}.json"
        if not snap_path.exists():
            if version is None:
                skipped_no_index += 1
            continue
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            if version is None:
                skipped_no_index += 1
            continue
        # 補強根拠バージョン (β/v1/v2) フィルタ (ユーザ指示 2026-06-30: 計測をバージョン毎に分離)。
        if version is not None and index_version_of(snap) != version:
            continue
        pop += 1
        result_path = RESULT_DIR / f"{safe}.json"
        result: dict[str, Any] = {}
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                result = {}
        meta = {
            "race_id": rid,
            "date": (race.get("_generated_at") or "")[:10],
            "venue": race.get("venue") or "",
            "race_no": race.get("race_no"),
            "race_type": race.get("race_type"),
            "shobu_score": race.get("shobu_score"),
            "n_runners": race.get("n_runners"),
        }
        detail, reason = _strategy_race_legs(
            snap, result, point_cost=point_cost, meta=meta)
        if reason == "no_index":
            skipped_no_index += 1
            continue
        if reason == "no_result":
            skipped_no_result += 1
            continue
        if reason != "ok" or detail is None:
            skipped_no_odds += 1     # 的中脚の払戻オッズ欠落
            continue

        races_n += 1
        for key, _label, _bt in STRATEGY_DEFS:
            s = detail["per"][key]
            if s["bets"] == 0:       # 0 脚 (≤4頭の複勝 / フィルタ見送り) = このレースは賭けない
                continue
            a = acc[key]
            a["races"] += 1
            a["races_hit"] += 1 if s["hit"] else 0      # レース単位の的中 (hit_rate 母数)
            a["bets"] += s["bets"]
            a["hits"] += s["hits"]
            a["stake"] += s["stake"]
            a["payout"] += s["payout"]
            a["per_race"].append((s["stake"], s["payout"]))
        r_ts = result.get("recorded_at")
        if isinstance(r_ts, str) and (last_updated_at is None or r_ts > last_updated_at):
            last_updated_at = r_ts
        races_detail.append(detail)

    strategies: list[dict[str, Any]] = []
    for key, label, bt in STRATEGY_DEFS:
        a = acc[key]
        roi = (a["payout"] / a["stake"]) if a["stake"] else 0.0
        roi_low, roi_high = _bootstrap_roi_ci(a["per_race"])
        # 的中率の母数は **レース数** (races_hit / races, ユーザ指示 2026-06-30)。
        hr_low, hr_high = _wilson_ci(a["races_hit"], a["races"])
        strategies.append({
            "key": key,
            "label": label,
            "bet_type": bt,
            "races": a["races"],
            "races_hit": a["races_hit"],
            "bets": a["bets"],
            "hits": a["hits"],
            "hit_rate": (a["races_hit"] / a["races"]) if a["races"] else 0.0,
            "hit_rate_ci_low": hr_low,
            "hit_rate_ci_high": hr_high,
            "stake": a["stake"],
            "payout": a["payout"],
            "net": a["payout"] - a["stake"],
            "roi": roi,
            "roi_ci_low": roi_low,
            "roi_ci_high": roi_high,
        })

    races_detail.sort(key=lambda r: (r.get("saved_at") or r.get("date") or ""), reverse=True)
    return {
        "point_cost": point_cost,
        "strategies": strategies,
        "races": races_n,
        "recommended_total": pop if version is not None else len(by_race),
        "version": version,
        "venue": venue,
        "skipped_no_index": skipped_no_index,
        "skipped_no_result": skipped_no_result,
        "skipped_no_odds": skipped_no_odds,
        "last_updated_at": last_updated_at,
        "sample_warning": races_n < 30,
        "races_detail": races_detail,
    }


def compute_shobu_strategies_pnl(point_cost: int = 100,
                                 version: str | None = None,
                                 venue: str | None = None) -> dict[str, Any]:
    """勝負レース (recommended) の Claude 指数 単純戦略くらべ 仮想収支 (ユーザ指示 2026-06-30)。"""
    return _strategies_pnl(point_cost, recommended_only=True, version=version, venue=venue)


def compute_indexed_strategies_pnl(point_cost: int = 100,
                                   version: str | None = None,
                                   venue: str | None = None) -> dict[str, Any]:
    """shobu が評価した全レース (recommended に限らない) の Claude 指数 単純戦略くらべ 仮想収支。

    ユーザ指示 (2026-06-30): 「単勝のみ・複勝のみ・指数1-2の馬連も計測して表示」。
    母集団は BOX 収支の indexed (compute_indexed_pnl) と揃える (shobu 評価レース全体)。
    `version` ("v1"/"v2"/"v3"/"β") を渡すと Claude 指数バージョン毎に分離 (ユーザ指示 2026-06-30)。
    `venue` ("nar"/"jra") で 地方/中央 に分離 (ユーザ指示 2026-07-05)。
    """
    return _strategies_pnl(point_cost, recommended_only=False, version=version, venue=venue)


# カードのバッジ表示用 短縮ラベル (STRATEGY_DEFS key → 表示名)。ダッシュボード仮想購入の的中表示。
STRATEGY_SHORT_LABELS = {
    "win1": "単勝1",
    "win2": "単勝2",
    "win3": "単勝3",
    "place1": "複勝1",
    "place2": "複勝2",
    "place3": "複勝3",
    "quinella12": "馬連1-2",
    "quinella13": "馬連1-3",
    "wide12": "ワイド1-2",
    "wide13": "ワイド1-3",
    "exacta12": "馬単1→2",
    "trifecta123": "3連単1→2→3",
    "trio123": "3連複1-2-3",
    "trio1234box": "3連複BOX",
    "wide123box": "ワイドBOX",
}


def hit_bet_labels(snap: dict[str, Any],
                   result: dict[str, Any]) -> list[dict[str, Any]] | None:
    """1 レースの **ダッシュボード仮想購入** の的中券種ラベル (ユーザ指示 2026-07-04)。

    勝負レースカード / 予測分析履歴カードに「どの券種が的中したか」を表示するための一覧。
    対象はダッシュボードで計測している仮想購入 = Claude 指数上位N頭 3連単BOX (`_box_race_pnl`)
    + 戦略くらべ (`_strategy_race_legs`, STRATEGY_DEFS)。**EV束/3連単束 (実弾) の的中とは無関係**。
    判定・フィルタ (≤1.1 見送り・複勝頭数ルール・同着 rank 判定) は計測本体と同じ共通ヘルパを
    使うので、ダッシュボードの races_detail と必ず一致する。

    返り値: 的中した仮想購入の [{key,label,payout}] (payout=¥100/脚換算・BOX 系は的中組合計)。
    的中ゼロは []。指数<3頭 / 結果未確定 / 的中脚オッズ欠落 (= ダッシュボード計測の分母外) は
    None (ラベル判定不能・カードは何も出さない)。
    """
    detail, reason = _strategy_race_legs(snap, result, point_cost=100, meta={})
    if reason != "ok" or detail is None:
        return None
    hits: list[dict[str, Any]] = []
    box_detail, box_reason = _box_race_pnl(snap, result, point_cost=100, box_size=5, meta={})
    if box_reason == "ok" and box_detail is not None and box_detail["hit"]:
        hits.append({"key": "box", "label": f"3連単BOX{box_detail['box']}頭",
                     "payout": box_detail["payout"]})
    for key, _label, _bt in STRATEGY_DEFS:
        s = detail["per"][key]
        if s["hit"]:
            hits.append({"key": key, "label": STRATEGY_SHORT_LABELS.get(key, key),
                         "payout": s["payout"]})
    return hits


def attach_hit_labels(doc: dict[str, Any]) -> dict[str, Any]:
    """shobu result doc の各レースに `hit_strategies` (仮想購入の的中券種ラベル) を付与する。

    scan file には結果が無い (結果はスキャン後に確定する) ので、配信時 (`get_shobu_result` /
    refresh 応答) に snapshot + result から都度計算して載せる。snapshot/result 欠落・判定不能は
    None のまま。doc を in-place 更新してそのまま返す。
    """
    for race in doc.get("races") or []:
        race["hit_strategies"] = None
        safe = _safe_race_id(race.get("race_id") or "")
        if safe is None:
            continue
        snap_path = PRED_DIR / f"{safe}.json"
        result_path = RESULT_DIR / f"{safe}.json"
        if not (snap_path.exists() and result_path.exists()):
            continue
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            res = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        race["hit_strategies"] = hit_bet_labels(snap, res)
    return doc


def _roi_block(races: int, races_hit: int, stake: int, payout: int) -> dict[str, Any]:
    """venue 内訳の 1 集計ブロック (races/races_hit/hit_rate/stake/payout/net/roi)。"""
    return {
        "races": races,
        "races_hit": races_hit,
        "hit_rate": (races_hit / races) if races else 0.0,
        "stake": stake,
        "payout": payout,
        "net": payout - stake,
        "roi": (payout / stake) if stake else 0.0,
    }


def compute_venue_breakdown(point_cost: int = 100,
                            version: str | None = None) -> dict[str, Any]:
    """**競馬場 (venue) 毎の内訳** 仮想収支 (ユーザ指示 2026-06-30: 競馬場毎にカードで内訳)。

    全体計測 (`compute_indexed_pnl` BOX + `compute_indexed_strategies_pnl` 戦略くらべ) の per-race
    detail を **venue で group 集計**して返す。市場由来 cutoff / version フィルタは下層が適用済。
    返り値: `{point_cost, version, venues: [{venue, n_races, box, strategies:[{key,label,...}]}]}`。
    venues は対象レース数の多い順。`box`/各 `strategies[]` は `_roi_block` 形 (races/hit_rate/roi/net 等)。
    的中率の母数はレース数 (BOX/戦略くらべ本体と同じ規約)。
    """
    box = _shobu_box_pnl(point_cost, recommended_only=False, version=version)
    strat = _strategies_pnl(point_cost, recommended_only=False, version=version)

    # venue → BOX 集計。
    box_by_venue: dict[str, dict[str, int]] = {}
    for r in box["races_detail"]:
        v = r.get("venue") or "(不明)"
        b = box_by_venue.setdefault(v, {"races": 0, "races_hit": 0, "stake": 0, "payout": 0})
        b["races"] += 1
        b["races_hit"] += 1 if r.get("hit") else 0
        b["stake"] += int(r.get("stake") or 0)
        b["payout"] += int(r.get("payout") or 0)

    # venue → 戦略 → 集計。
    strat_by_venue: dict[str, dict[str, dict[str, int]]] = {}
    venue_race_n: dict[str, int] = {}
    for r in strat["races_detail"]:
        v = r.get("venue") or "(不明)"
        venue_race_n[v] = venue_race_n.get(v, 0) + 1
        per = r.get("per") or {}
        sd = strat_by_venue.setdefault(v, {})
        for key, _label, _bt in STRATEGY_DEFS:
            s = per.get(key) or {}
            if not s.get("bets"):
                continue
            a = sd.setdefault(key, {"races": 0, "races_hit": 0, "stake": 0, "payout": 0})
            a["races"] += 1
            a["races_hit"] += 1 if s.get("hit") else 0
            a["stake"] += int(s.get("stake") or 0)
            a["payout"] += int(s.get("payout") or 0)

    label_of = {key: label for key, label, _bt in STRATEGY_DEFS}
    venues_all = set(box_by_venue) | set(strat_by_venue)
    venues: list[dict[str, Any]] = []
    for v in venues_all:
        b = box_by_venue.get(v, {"races": 0, "races_hit": 0, "stake": 0, "payout": 0})
        sd = strat_by_venue.get(v, {})
        strategies = [
            {"key": key, "label": label_of[key],
             **_roi_block(a["races"], a["races_hit"], a["stake"], a["payout"])}
            for key, _label, _bt in STRATEGY_DEFS
            if (a := sd.get(key)) is not None
        ]
        venues.append({
            "venue": v,
            "n_races": max(b["races"], venue_race_n.get(v, 0)),
            "box": _roi_block(b["races"], b["races_hit"], b["stake"], b["payout"]),
            "strategies": strategies,
        })
    venues.sort(key=lambda x: (x["n_races"], x["box"]["races"]), reverse=True)
    return {
        "point_cost": point_cost,
        "version": version,
        "venues": venues,
        "last_updated_at": box.get("last_updated_at") or strat.get("last_updated_at"),
    }


# ============================================================
# 市場一致シグナルの自動蓄積 (ユーザ指示 2026-06-30)
#   Claude#1 が市場1番人気と一致するか (consensus) で券種 ROI を分割し、
#   一致時の組合せ系(馬連等)・不一致時の3連複BOX が伸びる傾向を **時系列で蓄積** して
#   bootstrap CI が 0 から離れる (=確証) まで追う。結果取得ループから毎回 append される。
# ============================================================

MARKET_AGREEMENT_HISTORY = ROOT / "data" / "cache" / "market_agreement_history.jsonl"
_COMBO_KEYS = ["quinella12", "exacta12", "wide12"]          # 組合せ系
_HONMEI_KEYS = ["win1", "place1", "trio123", "trio1234box"]  # 本命系
# 追跡対象 (key, label, 構成戦略)。市場一致の効果を見たい少数に絞る (ユーザ「市場一致一本」)。
_AGREEMENT_TARGETS = [
    ("quinella12", "馬連 (指数1-2位)", ["quinella12"]),
    ("combo", "組合せ系 (馬連/馬単/ワイド)", _COMBO_KEYS),
    ("trio1234box", "3連複BOX (指数1-2-3-4)", ["trio1234box"]),
    ("honmei", "本命系 (単勝1/複勝1/3連複/BOX)", _HONMEI_KEYS),
]
# 市場指数の温度 (shobu._MARKET_INDEX_T / analyze.MARKET_INDEX_T のミラー)。
# market_index = 100·(1/odds)^(1/T) なので、逆に p_implied = (market_index/100)^T で
# 単勝オッズ由来の (未正規化) 勝率を復元できる。拮抗型/本命型の判定に使う。
_MARKET_INDEX_T = 1.5
# **本命型** (clear-favorite) の判定閾値: 市場1番人気の implied 勝率が2番人気の 2.0 倍以上なら
# 本命型 (1頭抜けている)、未満なら拮抗型 (competitive)。実測 (114R) で median≈1.76 のため
# 2.0 は「1番人気が2番人気の倍は堅い」の解釈が効く近-均衡点 (本命47R/拮抗67R)。閾値は固定 =
# 時系列の再現性を保つ (median split だと母集団増で分割点が動く)。ユーザ指示 2026-07-04。
_FAVORITE_RATIO_THRESHOLD = 2.0


def _market_by_number(snap: dict[str, Any]) -> dict[int, float]:
    """snapshot から 馬番→市場指数 (index_compare.market_index 優先・無ければ market_win_index)。"""
    out: dict[int, float] = {}
    for r in (snap.get("index_compare") or []):
        m = r.get("market_index")
        num = r.get("number")
        if m is not None and num is not None:
            try:
                out[int(num)] = float(m)
            except (TypeError, ValueError):
                continue
    if out:
        return out
    for k, v in (snap.get("market_win_index") or {}).items():
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _roi_of(pairs: list[tuple[int, int]]) -> float:
    s = sum(p[0] for p in pairs)
    return sum(p[1] for p in pairs) / s if s else 0.0


def _roi_delta_ci(high: list[tuple[int, int]], low: list[tuple[int, int]],
                  n_iter: int = 2000, seed: int = 42) -> tuple[float, float, float]:
    """ROI 差 Δ = ROI(high) − ROI(low) と bootstrap 95%CI (各群を再標本化・固定 seed で決定的)。"""
    if not high or not low:
        return (0.0, 0.0, 0.0)
    import random
    rng = random.Random(seed)
    base = _roi_of(high) - _roi_of(low)
    nh, nl = len(high), len(low)
    deltas = []
    for _ in range(n_iter):
        hs = [high[rng.randrange(nh)] for _ in range(nh)]
        ls = [low[rng.randrange(nl)] for _ in range(nl)]
        deltas.append(_roi_of(hs) - _roi_of(ls))
    deltas.sort()
    return base, deltas[int(0.025 * n_iter)], deltas[int(0.975 * n_iter)]


def _roi_ci(pairs: list[tuple[int, int]], n_iter: int = 2000,
            seed: int = 42) -> tuple[float, float]:
    """1 群の ROI の bootstrap 95%CI (レース単位ペアを再標本化・固定 seed で決定的)。

    マトリクス各セル (条件 × 券種) の ROI が break-even (=1.0) を超えて確証できるかの判定に使う
    (CI 下限 > 1.0 なら「その状況ではその買い方が確定的に +EV」)。空群は (0, 0)。
    """
    if not pairs:
        return (0.0, 0.0)
    import random
    rng = random.Random(seed)
    n = len(pairs)
    rois = []
    for _ in range(n_iter):
        s = [pairs[rng.randrange(n)] for _ in range(n)]
        rois.append(_roi_of(s))
    rois.sort()
    return rois[int(0.025 * n_iter)], rois[int(0.975 * n_iter)]


def _agreement_pairs(rows: list[dict[str, Any]], keys: list[str]) -> list[tuple[int, int]]:
    """レース毎に対象戦略の (Σstake, Σpayout) を1点に合算して返す。

    pooled ターゲット (combo/honmei) を脚単位で並べると、同一レースの脚は同じ top1/top2/着順を
    共有し強相関 (馬連が当たればワイドも当たりやすい) なのに bootstrap が iid 再標本化してしまい
    CI が過小 → 偽の★確証が出うる (2026-07-04 修正)。レース単位に合算すれば再標本化の単位=
    独立に近いレースになる。単一戦略ターゲットは 1 脚/レースなので数値は従来と同じ。
    """
    out: list[tuple[int, int]] = []
    for per in rows:
        stake = payout = 0
        for k in keys:
            s = per.get(k) or {}
            if s.get("bets"):
                stake += s["stake"]
                payout += s["payout"]
        if stake:
            out.append((stake, payout))
    return out


def _split_metrics(high_rows: list[dict[str, Any]], low_rows: list[dict[str, Any]],
                   *, high: str = "high", low: str = "low") -> list[dict[str, Any]]:
    """2 群 (high/low) の各ターゲット券種について ROI と 差Δ の bootstrap 95%CI を出す汎用ヘルパ。

    市場一致 (agree/disagree)・拮抗/本命・JRA/NAR いずれの二分割にも共通で使う。`high`/`low` で
    出力キーの接頭辞を変える (consensus は agree/disagree で後方互換を保つ)。`delta = high − low`。
    """
    metrics: list[dict[str, Any]] = []
    for key, label, keys in _AGREEMENT_TARGETS:
        hp = _agreement_pairs(high_rows, keys)
        lp = _agreement_pairs(low_rows, keys)
        delta, lo, hi = _roi_delta_ci(hp, lp)
        metrics.append({
            "key": key, "label": label,
            f"{high}_roi": _roi_of(hp), f"{low}_roi": _roi_of(lp),
            # _agreement_pairs がレース単位合算 (2026-07-04) なので legs = 1脚以上買ったレース数。
            f"{high}_legs": len(hp), f"{low}_legs": len(lp),
            "delta": delta, "delta_ci_low": lo, "delta_ci_high": hi,
            "significant": bool(hp and lp and (lo > 0 or hi < 0)),
        })
    return metrics


# マトリクスの条件軸 (key, 高ラベル=フラグ True 側, 低ラベル=False 側)。ユーザ指示 2026-07-04:
# 「①②③で分けるのでなくマトリクス表にして条件を組み合わせ、状況毎の最良の買い方を見る」。
# 3 軸 = 2^3 = 8 状況 (行)。列は _AGREEMENT_TARGETS (馬連/組合せ/3連複BOX/本命系)。
_MATRIX_DIMS = [
    ("consensus", "一致", "不一致"),   # Claude#1 == 市場1番人気 か
    ("style", "拮抗型", "本命型"),      # 市場 top2 の implied 勝率比 < / ≥ _FAVORITE_RATIO_THRESHOLD
    ("venue", "JRA", "NAR"),           # 中央 / 地方 (banei は NAR にまとめる)
]
# このレース数未満のセルは「最良(推奨)」や「確証★」を出さない (サンプル不足で ROI が信頼できない)。
# 実測: 3軸 8 セルのうち JRA 系は各 1-5R しか無く、ここを highlight すると overfit になる (honest 表示)。
_MATRIX_SAMPLE_FLOOR = 8


def _matrix_cells(rows: list[dict[str, Any]], floor: int) -> tuple[list[dict[str, Any]], str | None]:
    """あるセル (条件で絞った per リスト) の各ターゲット券種 ROI + bootstrap CI を出す。

    返り値 `(cells, best_key)`。cells は _AGREEMENT_TARGETS 順。`confirmed` = レース数 ≥ floor かつ
    ROI CI 下限 > 1.0 (その状況でその買い方が確定的に +EV)。`best_key` = floor 以上のセルの中で
    ROI 最大の券種 (= その状況の最良の買い方)、floor 以上が無ければ None (サンプル不足)。
    """
    cells: list[dict[str, Any]] = []
    for key, label, keys in _AGREEMENT_TARGETS:
        pairs = _agreement_pairs(rows, keys)
        lo, hi = _roi_ci(pairs)
        cells.append({
            "key": key, "label": label,
            "roi": _roi_of(pairs), "legs": len(pairs),
            "roi_ci_low": lo, "roi_ci_high": hi,
            "confirmed": bool(len(pairs) >= floor and lo > 1.0),
        })
    eligible = [c for c in cells if c["legs"] >= floor]
    best = max(eligible, key=lambda c: c["roi"])["key"] if eligible else None
    return cells, best


def _race_features(idx: dict[int, float], mkt: dict[int, float],
                   snap: dict[str, Any] | None = None) -> dict[str, float | None]:
    """Claude 指数上位3頭 + 市場の荒れ具合の **発走前観測可能な数値特徴量** (ユーザ指示 2026-07-05:
    「上位3頭の指数と市場との差・1,2,3の開き・4頭目との開き・市場のオッズの開き (荒れ具合) から
    どの券種が回収率が高くなるか研究中シグナルに入れて」)。

    プレレジルールの `features` 条件 (`_rule_matches`) と読み取り専用スイープ
    (`scripts/signal_feature_sweep.py`) が共有する。計算不能な特徴量は None (= その特徴量を
    要求するルールは発火しない)。同点タイブレークは他と同じ (-指数, 馬番昇順)。

      - gap12 / gap23: Claude 指数 1-2位 / 2-3位 の開き (指数ポイント)
      - gap34: Claude 3位と4位の開き = 上位3頭パックと残りの分離度 (指数4頭未満は None)
      - top3_rank_gap: Claude 上位3頭の Σ(市場順位 − Claude順位)。正 = 市場が Claude 上位勢を
        過小評価 (shobu 基準B の rank_gap を上位3頭に拡張したもの)
      - top3_idx_diff: Claude 上位3頭の mean(claude_index − market_index) (同一 0-100 尺度の数値差)
      - fav_odds: 市場1番人気の単勝オッズ復元値 (100/market_index)^T。高い = 突出人気不在 = 荒れ模様
      - top3_conc: 市場 implied 勝率の上位3頭への集中度 (Σtop3 p / Σall p)。低い = 混戦 (荒れ)
      - pw_top1/2/3: Claude 上位1/2/3位馬の 単勝オッズ ÷ 複勝オッズ (snap の bet_tables から。
        本命-大穴バイアス (FL bias) の per-horse シグナル: 高い = 市場が「勝ち切らないが
        絡む」(3着型) と見る馬。複勝はレンジ下限なので比は系統的にやや高め (レース間で一貫)。
        bet_tables が無い/欠落は None (2026-07-06 追加)
    """
    c_sorted = sorted(idx.items(), key=lambda kv: (-kv[1], kv[0]))
    gap12 = c_sorted[0][1] - c_sorted[1][1] if len(c_sorted) >= 2 else None
    gap23 = c_sorted[1][1] - c_sorted[2][1] if len(c_sorted) >= 3 else None
    gap34 = c_sorted[2][1] - c_sorted[3][1] if len(c_sorted) >= 4 else None
    m_sorted = sorted(mkt.items(), key=lambda kv: (-kv[1], kv[0]))
    m_rank = {num: i + 1 for i, (num, _mi) in enumerate(m_sorted)}
    top3_rank_gap: float | None = None
    top3_idx_diff: float | None = None
    top3 = c_sorted[:3]
    if len(top3) == 3 and all(num in mkt for num, _v in top3):
        top3_rank_gap = float(sum(m_rank[num] - (k + 1) for k, (num, _v) in enumerate(top3)))
        top3_idx_diff = sum(v - mkt[num] for num, v in top3) / 3.0
    fav_odds: float | None = None
    top3_conc: float | None = None
    if m_sorted and m_sorted[0][1] > 0:
        fav_odds = (100.0 / m_sorted[0][1]) ** _MARKET_INDEX_T
    probs = [(mi / 100.0) ** _MARKET_INDEX_T for _num, mi in m_sorted if mi > 0]
    if len(probs) >= 3 and sum(probs) > 0:
        top3_conc = sum(probs[:3]) / sum(probs)
    # FL バイアス: Claude 上位3頭の 単勝/複勝 オッズ比 (bet_tables の実オッズ)。
    pw: dict[str, float | None] = {"pw_top1": None, "pw_top2": None, "pw_top3": None}
    if snap is not None:
        win_o = {k[0]: v for k, v in _snap_combo_odds(snap, "win").items() if v > 0}
        plc_o = {k[0]: v for k, v in _snap_combo_odds(snap, "place").items() if v > 0}
        for i, (num, _v) in enumerate(c_sorted[:3]):
            w_o, p_o = win_o.get(num), plc_o.get(num)
            if w_o and p_o:
                pw[f"pw_top{i + 1}"] = w_o / p_o
    return {
        "gap12": gap12, "gap23": gap23, "gap34": gap34,
        "top3_rank_gap": top3_rank_gap, "top3_idx_diff": top3_idx_diff,
        "fav_odds": fav_odds, "top3_conc": top3_conc, **pw,
    }


def _tagged_eval_races(point_cost: int = 100) -> list[dict[str, Any]]:
    """市場非依存 (β除外) の shobu 評価レースに 発走前条件タグ + 戦略脚 を付けた共通レコード列。

    `compute_market_agreement` (買い方マトリクス) と `compute_signal_rules` (プレレジ検証 +
    walk-forward ガードレール) が共有するローダ。各レコード:
      - rid / ts (start_at unix, 無ければ 0) / date (発走日の JST YYYY-MM-DD、ts 無しは
        スキャン日 _generated_at で補完) / recorded_at (結果保存時刻)
      - scored_pre_start: 指数採点時刻 (_scored_at) < 発走時刻 か (発走後の再score =
        hindsight 汚染の可能性がある指数を prospective 確証から弾くガード。ts 無しは False)
      - flags: {consensus: Claude#1==市場1番人気 (同点は馬番昇順の明示タイブレーク),
        style: 拮抗型=True, venue: JRA=True}
      - n_runners / per (Claude 指数順の戦略脚) / mper (市場人気順で同じ買い方、組めなければ None)
    **ts 昇順で返す** (walk-forward がそのまま時系列で歩ける)。
    """
    import datetime as _dt
    by_race = _shobu_eval_races(False)
    records: list[dict[str, Any]] = []
    for rid, race in by_race.items():
        safe = _safe_race_id(rid)
        if safe is None:
            continue
        snap_path = PRED_DIR / f"{safe}.json"
        result_path = RESULT_DIR / f"{safe}.json"
        if not snap_path.exists() or not result_path.exists():
            continue
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _scored_at(snap) < MARKET_INDEPENDENT_CUTOFF_ISO_JST:
            continue   # 市場由来 (β) は除外
        idx = _claude_index_by_number(snap)
        mkt = _market_by_number(snap)
        if len(idx) < 3 or len(mkt) < 2:
            continue
        detail, reason = _strategy_race_legs(snap, result, point_cost=point_cost,
                                             meta={"race_id": rid})
        if reason != "ok" or detail is None:
            continue
        # 3 条件でタグ付け (フラグ True → 高ラベル側)。consensus の同点タイブレークは
        # _strategy_race_legs / market_ranked と同じ **(-指数, 馬番昇順)** に揃える
        # (旧 max(dict) は index_compare の行順依存 = Claude 指数降順で同点が agree 側に
        # 倒れ、market_baseline と別の馬を市場#1 とみなす矛盾があった。2026-07-05 レビュー修正)。
        claude_top = min(idx.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        market_top = min(mkt.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        agree = claude_top == market_top
        ms = sorted(mkt.values(), reverse=True)
        p1 = (ms[0] / 100.0) ** _MARKET_INDEX_T
        p2 = (ms[1] / 100.0) ** _MARKET_INDEX_T
        competitive = not (p2 > 0 and (p1 / p2) >= _FAVORITE_RATIO_THRESHOLD)  # 拮抗型=True
        jra = race.get("race_type") == "jra"
        # 市場人気ベースライン: 市場指数順の上位馬で同じ買い方 (オッズ/フィルタ/的中は共通ロジック)。
        mper: dict[str, Any] | None = None
        market_ranked = [num for num, _mi in sorted(mkt.items(), key=lambda kv: (-kv[1], kv[0]))]
        if len(market_ranked) >= 3:
            mdetail, mreason = _strategy_race_legs(
                snap, result, point_cost=point_cost, meta={"race_id": rid},
                ranking=market_ranked)
            if mreason == "ok" and mdetail is not None:
                mper = mdetail["per"]
        ts = race.get("start_at") or 0
        if ts:
            start_dt = _dt.datetime.fromtimestamp(
                ts, _dt.timezone(_dt.timedelta(hours=9)))
            date = start_dt.date().isoformat()
            start_iso = start_dt.replace(tzinfo=None).isoformat(timespec="seconds")
        else:
            date = (race.get("_generated_at") or "")[:10]
            start_iso = ""
        # 指数が発走前に生成されたか (hindsight ガード)。発走後の再score (claude_eval リトライや
        # 過去レースの手動 analyze) は web 検索が確定結果を拾い得るため、prospective (確証★の
        # 母集団) には「採点時刻 < 発走時刻」を要求する (2026-07-05 レビュー修正)。start_at 不明は
        # 検証不能 → False (保守的に prospective から除外)。
        scored = _scored_at(snap)
        r_ts = result.get("recorded_at")
        records.append({
            "rid": rid,
            "ts": ts,
            "date": date,
            "recorded_at": r_ts if isinstance(r_ts, str) else None,
            "scored_pre_start": bool(start_iso) and bool(scored) and scored < start_iso,
            "flags": {"consensus": agree, "style": competitive, "venue": jra},
            "n_runners": detail.get("n_runners") or 0,
            # 上位3頭ギャップ + 荒れ具合 + FL バイアスの数値特徴量 (プレレジ features 条件用)。
            "features": _race_features(idx, mkt, snap),
            "per": detail["per"],
            "mper": mper,
        })
    records.sort(key=lambda r: (r["ts"], r["rid"]))
    return records


def compute_market_agreement(point_cost: int = 100) -> dict[str, Any]:
    """**市場一致シグナル + 買い方マトリクス**: 観測可能な発走前条件の組合せ毎に最良の買い方を見る。

    市場非依存 (β除外) の shobu 評価レースを、3 つの二値条件でタグ付けする:
      - consensus: Claude 1位 == 市場1番人気 (一致) か
      - style: 市場 top2 の implied 勝率比が `_FAVORITE_RATIO_THRESHOLD` 倍未満=拮抗型 / 以上=本命型
      - venue: JRA (中央) か NAR (地方・banei 含む) か
    `matrix` はこの 2^3=8 状況を行、`_AGREEMENT_TARGETS` (馬連/組合せ/3連複BOX/本命系) を列に、
    各セルの ROI と bootstrap CI を出し、状況毎の最良の買い方 (`best_key`) と確証 (CI下限>1.0) を示す。
    参考行として `overall` (条件なし全レース) と `market_baseline` (市場人気順で同じ買い方) を付ける。

    `metrics` (Claude#1==市場1番人気の agree/disagree 1次元スプリット + Δ CI) は後方互換で残す
    (per-race 買い方ガイド `web/lib/betGuide.ts` が現在値を参照する)。
    `append_market_agreement_history` が結果取得ごとに現在値を時系列保存し確証まで蓄積する。

    ⚠ **このマトリクスの in-sample best セルをそのまま追従しても勝てない** ことが walk-forward
    (`compute_signal_rules` の walkforward ブロック, look-ahead なし) で実測済 (2026-07-05,
    105R: 追従 ROI 48-66% < 馬連固定 78%)。セルの数字は「発見の場」であり、行動に移すのは
    プレレジ (`SIGNAL_RULES`) で登録後データの確証が取れたルールのみ。
    """
    agree_rows: list[dict[str, Any]] = []
    disagree_rows: list[dict[str, Any]] = []
    tagged: list[tuple[dict[str, bool], dict[str, Any]]] = []   # (条件フラグ, per) 全レース
    market_rows: list[dict[str, Any]] = []     # 市場人気ベースライン (市場指数順で同じ買い方)
    last_updated_at: str | None = None
    for rec in _tagged_eval_races(point_cost):
        per = rec["per"]
        tagged.append((rec["flags"], per))
        (agree_rows if rec["flags"]["consensus"] else disagree_rows).append(per)
        if rec["mper"] is not None:
            market_rows.append(rec["mper"])
        r_ts = rec["recorded_at"]
        if isinstance(r_ts, str) and (last_updated_at is None or r_ts > last_updated_at):
            last_updated_at = r_ts

    # マトリクス: 全条件組合せ (2^3) を行に。各行は条件で絞った per リストのセル ROI。
    from itertools import product
    floor = _MATRIX_SAMPLE_FLOOR
    matrix_rows: list[dict[str, Any]] = []
    for combo in product((True, False), repeat=len(_MATRIX_DIMS)):
        want = {k: v for (k, _hi, _lo), v in zip(_MATRIX_DIMS, combo)}
        sel = [per for flags, per in tagged if all(flags[k] == v for k, v in want.items())]
        cells, best = _matrix_cells(sel, floor)
        matrix_rows.append({
            "signature": [bool(want[k]) for k, _hi, _lo in _MATRIX_DIMS],
            "labels": [hi if want[k] else lo for k, hi, lo in _MATRIX_DIMS],
            "n": len(sel), "cells": cells, "best_key": best,
        })
    matrix_rows.sort(key=lambda r: -r["n"])   # サンプルの多い状況を上に (JRA 疎な行は下へ)
    overall_cells, overall_best = _matrix_cells([per for _f, per in tagged], floor)
    mbase_cells, _mbest = _matrix_cells(market_rows, floor)

    n = len(agree_rows) + len(disagree_rows)
    return {
        "races": n,
        "agree_n": len(agree_rows),
        "disagree_n": len(disagree_rows),
        # consensus 1次元スプリット (betGuide.ts が参照・後方互換)。
        "metrics": _split_metrics(agree_rows, disagree_rows, high="agree", low="disagree"),
        "matrix": {
            "dims": [{"key": k, "high_label": hi, "low_label": lo}
                     for k, hi, lo in _MATRIX_DIMS],
            "targets": [{"key": k, "label": lbl} for k, lbl, _ in _AGREEMENT_TARGETS],
            "sample_floor": floor,
            "rows": matrix_rows,
            "overall": {"n": len(tagged), "cells": overall_cells, "best_key": overall_best},
            "market_baseline": {"n": len(market_rows), "cells": mbase_cells},
        },
        "last_updated_at": last_updated_at,
        "sample_warning": n < 50,
    }


def append_market_agreement_history() -> dict[str, Any] | None:
    """現在の市場一致シグナルを計算し、レース数が前回より増えていれば history (jsonl) に追記。

    結果取得ループ (api/main.py ResultAutoFetcher._run_once) から毎回呼ばれ、レースが溜まるごとに
    シグナル (ROI 差と CI) の推移を残す → CI が 0 から離れれば確証。`races` が前回 entry と同じなら
    no-op (新しい結果が無い)。返り値は追記した row (no-op は None)。
    """
    m = compute_market_agreement()
    if m["races"] == 0:
        return None
    last_races = None
    if MARKET_AGREEMENT_HISTORY.exists():
        try:
            lines = MARKET_AGREEMENT_HISTORY.read_text(encoding="utf-8").splitlines()
            if lines:
                last_races = json.loads(lines[-1]).get("races")
        except (OSError, json.JSONDecodeError, ValueError):
            last_races = None
    if last_races == m["races"]:
        return None
    import datetime as _dt
    row = {
        "recorded_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "races": m["races"], "agree_n": m["agree_n"], "disagree_n": m["disagree_n"],
        "metrics": [
            {"key": x["key"], "agree_roi": round(x["agree_roi"], 4),
             "disagree_roi": round(x["disagree_roi"], 4),
             "delta": round(x["delta"], 4),
             "delta_ci_low": round(x["delta_ci_low"], 4),
             "delta_ci_high": round(x["delta_ci_high"], 4),
             "significant": x["significant"]}
            for x in m["metrics"]
        ],
        # 買い方マトリクス (2026-07-04): 状況 × 券種 の ROI/確証 を compact に蓄積 (確証 セルの推移を追う)。
        "matrix": [
            {"labels": r["labels"], "n": r["n"], "best_key": r["best_key"],
             "cells": [{"key": c["key"], "roi": round(c["roi"], 4), "legs": c["legs"],
                        "roi_ci_low": round(c["roi_ci_low"], 4),
                        "confirmed": c["confirmed"]}
                       for c in r["cells"]]}
            for r in m["matrix"]["rows"]
        ],
    }
    MARKET_AGREEMENT_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(MARKET_AGREEMENT_HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def market_agreement_history(limit: int = 200) -> list[dict[str, Any]]:
    """蓄積済の市場一致シグナル時系列 (古→新、最大 limit 件)。"""
    if not MARKET_AGREEMENT_HISTORY.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in MARKET_AGREEMENT_HISTORY.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-limit:]


# ─── プレレジ (事前登録) シグナルルール + walk-forward ガードレール (2026-07-05) ─────────
#
# 「研究中シグナルで回収率を上げる」正攻法: マトリクスの in-sample best セルは bin-selection
# (trio1234box 失効・馬連202%セルの v2 時代 36% 崩壊と同型) なので**追従しない**。代わりに
#   1. 発見したルールを **定義固定 (プレレジ)** して registered_at を刻む
#   2. **登録日以降のレースのみ** で ROI + bootstrap CI を蓄積 (= 真の out-of-sample)
#   3. prospective n ≥ SIGNAL_RULE_MIN_CONFIRM かつ CI 下限 > 1.0 で初めて「確証★」
#   4. CI 上限 < 1.0 になったら「破綻」= ルール棄却 (負けを引きずらない)
# walk-forward ブロックは「マトリクスをそのまま追従したら?」の正直な成績を常時併記し、
# セル追従が機能しない事実をダッシュボードで見えるようにする (誤用ガードレール)。

SIGNAL_RULES_HISTORY = ROOT / "data" / "cache" / "signal_rules_history.jsonl"

SIGNAL_RULE_MIN_CONFIRM = 30   # 確証 (CI下限>1.0) に必要な登録後レース数
SIGNAL_RULE_MIN_BROKEN = 20    # 破綻 (CI上限<1.0) を宣言できる登録後レース数

# ルール定義は **凍結** (registered_at を変えない限り条件・戦略を書き換えない)。変更したい場合は
# 新しい key で登録し直す (= プレレジのやり直し)。discovery は発見時 in-sample の参考値。
SIGNAL_RULES: list[dict[str, Any]] = [
    {"key": "place2_bigfield", "label": "複勝2 × 多頭数",
     "strategy": "place2", "condition_label": "出走12頭以上",
     "registered_at": "2026-07-05", "min_runners": 12,
     "discovery": "発見時105R: ROI 123% (n=31, drop-best 105%) — 唯一 drop-best 後も100%超"},
    {"key": "place2_bigfield_agree", "label": "複勝2 × 多頭数 × 市場一致",
     "strategy": "place2", "condition_label": "出走12頭以上 かつ Claude#1=市場1番人気",
     "registered_at": "2026-07-05", "min_runners": 12, "consensus": True,
     "discovery": "発見時105R: ROI 162% (n=15, drop-best 126%)"},
    {"key": "place1_consensus", "label": "複勝1 × 市場一致",
     "strategy": "place1", "condition_label": "Claude#1=市場1番人気",
     "registered_at": "2026-07-05", "consensus": True,
     "discovery": "発見時105R: ROI 97% (n=15, 前後半で安定。市場#1複勝全体80%・"
                  "Claude不一致側の人気馬複勝74%に対し一致選別の付加価値)"},
    {"key": "win1_smallfield", "label": "単勝1 × 少頭数",
     "strategy": "win1", "condition_label": "出走8頭以下",
     "registered_at": "2026-07-05", "max_runners": 8,
     "discovery": "発見時105R: ROI 123% (n=12, drop-best 92% = 単発依存の疑い)"},
    {"key": "quinella12_alive", "label": "馬連1-2 × 死にセル回避",
     "strategy": "quinella12", "condition_label": "拮抗型×市場不一致 のレースは見送り",
     "registered_at": "2026-07-05", "skip_dead_cell": True,
     "discovery": "発見時105R: 回避後 ROI 97% (n=66) vs 死にセル 47% — 見送り規律"},
    {"key": "quinella12_agree_honmei_nar", "label": "馬連1-2 × 一致×本命型×NAR",
     "strategy": "quinella12", "condition_label": "Claude#1=市場1番人気 かつ 本命型 かつ NAR",
     "registered_at": "2026-07-05", "consensus": True, "style": False, "venue": False,
     "discovery": "発見時105R: ROI 202% だが drop-best 106%・v2時代36% (マトリクス紙面上の主役の追試)"},
    # --- 上位3頭ギャップ + 荒れ具合の特徴量ルール (ユーザ指示 2026-07-05, `_race_features`) ---
    # 発見は scripts/signal_feature_sweep.py (108R in-sample・固定閾値グリッド)。条件は全て
    # 発走前観測可能な数値特徴量の min/max。以降は登録後データのみで確証判定 (定義凍結)。
    {"key": "place2_top3undervalued", "label": "複勝2 × 上位3頭を市場が過小評価",
     "strategy": "place2",
     "condition_label": "Claude上位3頭の Σ(市場順位−Claude順位) ≥ 5",
     "registered_at": "2026-07-05",
     "features": {"top3_rank_gap": {"min": 5}},
     "discovery": "発見時108R: ROI 110% (n=38, drop-best 94%, 前半113%/後半106%と唯一安定・"
                  "市場人気基準67%)。place2_bigfield と母集団は部分重複 (21/47R) の別条件"},
    {"key": "wide13_top3consensus", "label": "ワイド1-3 × 上位3頭が市場と一致",
     "strategy": "wide13",
     "condition_label": "Claude上位3頭の Σ(市場順位−Claude順位) ≤ 0",
     "registered_at": "2026-07-05",
     "features": {"top3_rank_gap": {"max": 0}},
     "discovery": "発見時108R: ROI 130% (n=20, drop-best 114%, 前半82%/後半177%・市場人気基準65%。"
                  "市場と上位勢の見立てが揃う時 Claude の並び順が価値を持つ仮説)"},
    {"key": "win1_rough_market", "label": "単勝1 × 荒れ模様 (突出人気不在)",
     "strategy": "win1",
     "condition_label": "市場1番人気の復元オッズ ≥ 2.5倍",
     "registered_at": "2026-07-05",
     "features": {"fav_odds": {"min": 2.5}},
     "discovery": "発見時108R: ROI 119% (n=32, drop-best 95% だが 前半179%/後半58%と不安定・"
                  "市場人気基準58%。荒れ具合系の代表として登録)"},
    {"key": "place3_toppack_tight", "label": "複勝3 × Claude 2-3位が拮抗",
     "strategy": "place3",
     "condition_label": "Claude指数の 2位−3位差 ≤ 2 (3位は実質2位級)",
     "registered_at": "2026-07-05",
     "features": {"gap23": {"max": 2}},
     "discovery": "発見時108R: ROI 159% (n=17, drop-best 119%, 前半258%/後半71%と不安定 — "
                  "1,2,3の開き系の代表として n極小のまま参考登録)"},
    # --- FL バイアス (単勝/複勝オッズ比 = 市場の「勝ち切り型/3着型」評価) ルール
    #     (ユーザ指示 2026-07-05「他に回収率を上げられることは」→ pw 特徴量 sweep から 2 本,
    #      2026-07-06 プレレジ。特徴量は `_race_features` の pw_top* = bet_tables 実オッズ比) ---
    {"key": "quinella13_top2winner", "label": "馬連1-3 × 2位が勝ち切り型",
     "strategy": "quinella13",
     "condition_label": "Claude2位馬の 単勝/複勝オッズ比 ≤ 3.0 (市場が勝ち切り型と評価)",
     "registered_at": "2026-07-06",
     "features": {"pw_top2": {"max": 3.0}},
     "discovery": "発見時108R: ROI 141% (n=39, 前半141%/後半142%と安定・市場人気基準52%。"
                  "ただし drop-best 92% = 単発寄与が大きい)"},
    {"key": "place2_top2placer", "label": "複勝2 × 2位が3着型",
     "strategy": "place2",
     "condition_label": "Claude2位馬の 単勝/複勝オッズ比 ≥ 4.0 (市場が「絡むが勝ち切らない」と評価)",
     "registered_at": "2026-07-06",
     "features": {"pw_top2": {"min": 4.0}},
     "discovery": "発見時108R: ROI 102% (n=47, drop-best 90%, 前半126%/後半79%。"
                  "FLバイアス系の対側・市場人気基準64%に対する付加価値が主眼)"},
]

# --- 券種比較グリッド (ユーザ指示 2026-07-05「単勝1,単勝2,単勝3,複勝1,複勝2,複勝3,馬連1-2,
# 馬連1-3,ワイド1-2,ワイド1-3 の順で並べ、条件の定義は固定のものにして比較」) ---
# 10 券種 × 固定 3 条件を一括プレレジし、**全券種を同じ条件の土俵で比較**できるようにする。
# 個別発見のルール (上の手書き分) とは別 key ("grid_" 接頭) で、判定・蓄積・凍結規約は同一。
# win2/win3/quinella13 はこのグリッドのために STRATEGY_DEFS へ追加した新戦略 (同日)。
_GRID_STRATEGIES = ["win1", "win2", "win3", "place1", "place2", "place3",
                    "quinella12", "quinella13", "wide12", "wide13"]
_GRID_CONDITIONS: list[tuple[str, str, dict[str, Any]]] = [
    ("all", "無条件 (全評価レース)", {}),
    ("agree", "市場一致 (Claude#1=市場1番人気)", {"consensus": True}),
    ("rough", "荒れ模様 (1番人気の復元オッズ ≥ 2.5)",
     {"features": {"fav_odds": {"min": 2.5}}}),
]
for _sk in _GRID_STRATEGIES:
    for _ck, _clabel, _cond in _GRID_CONDITIONS:
        SIGNAL_RULES.append({
            "key": f"grid_{_sk}_{_ck}",
            "label": f"{_sk} × {_ck}",
            "strategy": _sk,
            "condition_label": _clabel,
            "registered_at": "2026-07-05",
            "discovery": "券種比較グリッド (固定条件の一括プレレジ 2026-07-05)",
            **_cond,
        })
del _sk, _ck, _clabel, _cond


def _rule_matches(rule: dict[str, Any], rec: dict[str, Any]) -> bool:
    """プレレジルールの発走前条件がレコードに合致するか (すべて発走前に観測可能な条件のみ)。"""
    f = rec["flags"]
    n = rec.get("n_runners") or 0
    for dim in ("consensus", "style", "venue"):
        want = rule.get(dim)
        if want is not None and f[dim] != want:
            return False
    mn = rule.get("min_runners")
    if mn is not None and n < mn:
        return False
    mx = rule.get("max_runners")
    if mx is not None and (n <= 0 or n > mx):
        return False
    if rule.get("skip_dead_cell") and (f["style"] and not f["consensus"]):
        return False   # 死にセル (拮抗型 × 市場不一致) は見送り
    # 数値特徴量の min/max 条件 (`_race_features` の gap12/gap34/top3_rank_gap/fav_odds/
    # top3_conc 等, 2026-07-05)。特徴量が計算不能 (None) のレースはルール不発火 = 保守的。
    for name, cond in (rule.get("features") or {}).items():
        v = (rec.get("features") or {}).get(name)
        if v is None:
            return False
        if "min" in cond and v < cond["min"]:
            return False
        if "max" in cond and v > cond["max"]:
            return False
    return True


def _signal_stats(pairs: list[tuple[int, int]], *, with_drop_best: bool = False) -> dict[str, Any]:
    """レース単位 (stake, payout) 列の ROI 統計ブロック (+CI, 任意で drop-best)。"""
    stake = sum(p[0] for p in pairs)
    payout = sum(p[1] for p in pairs)
    roi = payout / stake if stake else 0.0
    lo, hi = _roi_ci(pairs)
    out = {
        "races": len(pairs),
        "hits": sum(1 for p in pairs if p[1] > 0),
        "stake": stake,
        "payout": payout,
        "roi": roi,
        "roi_ci_low": lo,
        "roi_ci_high": hi,
    }
    if with_drop_best:
        # 最大払戻1レースを除いた ROI (単発ジャックポット依存の検出)。
        if len(pairs) >= 2:
            best_i = max(range(len(pairs)), key=lambda i: pairs[i][1] - pairs[i][0])
            out["drop_best_roi"] = _roi_of([p for i, p in enumerate(pairs) if i != best_i])
        else:
            out["drop_best_roi"] = 0.0
    return out


def _rule_status(prospective: dict[str, Any]) -> tuple[str, str]:
    """登録後 (prospective) 統計からルール状態を判定する。

    確証★ = n ≥ SIGNAL_RULE_MIN_CONFIRM かつ ROI CI 下限 > 1.0 (登録後データだけで +EV 確定)。
    破綻 = n ≥ SIGNAL_RULE_MIN_BROKEN かつ CI 上限 < 1.0 (登録後データで -EV 確定 → 棄却)。
    有望 = n ≥ MIN_CONFIRM かつ ROI > 1.0 だが CI 未達。それ以外は蓄積中。
    """
    n = prospective["races"]
    if n >= SIGNAL_RULE_MIN_CONFIRM and prospective["roi_ci_low"] > 1.0:
        return "confirmed", "確証★"
    if n >= SIGNAL_RULE_MIN_BROKEN and prospective["roi_ci_high"] < 1.0:
        return "broken", "破綻"
    if n >= SIGNAL_RULE_MIN_CONFIRM and prospective["roi"] > 1.0:
        return "promising", "有望"
    return "accumulating", "蓄積中"


def _walkforward_matrix(records: list[dict[str, Any]], *, floor: int,
                        fallback_overall: bool) -> dict[str, Any]:
    """買い方マトリクス追従の walk-forward 成績 (look-ahead なし・O(N) 逐次更新)。

    各レース t で「t より前のレースのみ」からセル (consensus×style×venue) 毎の
    ターゲット券種 ROI を集計し、`floor` レース以上あるターゲットのうち ROI 最大を賭ける。
    `fallback_overall=True` はセルのサンプル不足時に全体 (条件なし) の best へフォールバック。
    accumulator は**賭けた後に**当該レースを反映するので未来情報は一切使わない。
    """
    acc_new = lambda: {k: [0, 0, 0] for k, _l, _ks in _AGREEMENT_TARGETS}  # n/stake/payout
    cell_acc: dict[tuple[bool, bool, bool], dict[str, list[int]]] = {}
    overall_acc = acc_new()

    def _best(acc: dict[str, list[int]]) -> str | None:
        best_key, best_roi = None, -1.0
        for key, _lbl, _keys in _AGREEMENT_TARGETS:
            n, stk, pay = acc[key]
            if n < floor or stk <= 0:
                continue
            roi = pay / stk
            if roi > best_roi:
                best_key, best_roi = key, roi
        return best_key

    keys_of = {k: ks for k, _l, ks in _AGREEMENT_TARGETS}
    pairs: list[tuple[int, int]] = []
    chosen: dict[str, int] = {}
    for rec in records:
        f = rec["flags"]
        sig = (f["consensus"], f["style"], f["venue"])
        acc = cell_acc.setdefault(sig, acc_new())
        pick = _best(acc)
        if pick is None and fallback_overall:
            pick = _best(overall_acc)
        if pick is not None:
            got = _agreement_pairs([rec["per"]], keys_of[pick])
            if got:
                pairs.append(got[0])
                chosen[pick] = chosen.get(pick, 0) + 1
        # 賭けた後に反映 (look-ahead 防止)。
        for key, _lbl, ks in _AGREEMENT_TARGETS:
            got = _agreement_pairs([rec["per"]], ks)
            if got:
                for a in (acc[key], overall_acc[key]):
                    a[0] += 1
                    a[1] += got[0][0]
                    a[2] += got[0][1]
    stats = _signal_stats(pairs, with_drop_best=True)
    stats["chosen"] = dict(sorted(chosen.items(), key=lambda kv: -kv[1]))
    return stats


def compute_signal_rules(point_cost: int = 100) -> dict[str, Any]:
    """プレレジ済シグナルルールの検証状況 + walk-forward ガードレール (2026-07-05)。

    各ルールについて:
      - insample: 全期間 (発見期間込み) の ROI — **参考値** (発見に使ったデータなので楽観)
      - prospective: **registered_at 以降の発走日 かつ 指数が発走前に生成された (scored_pre_start)
        レースのみ** の ROI + CI — 確証判定はこちらだけ
      - market_baseline: 同条件で市場人気順に同じ買い方をした ROI (Claude 指数の付加価値の基準線)
      - status: accumulating / promising / confirmed★ / broken (判定は `_rule_status`)
    `dead_cell` は見送り規律 (拮抗型×市場不一致) の根拠表示用、`walkforward` はマトリクス
    best セル追従の正直な成績 (これが 100% を大きく割る限りセル追従は機能していない)。
    """
    records = _tagged_eval_races(point_cost)
    rules_out: list[dict[str, Any]] = []
    for rule in SIGNAL_RULES:
        keys = [rule["strategy"]]
        matched = [r for r in records if _rule_matches(rule, r)]
        all_pairs = [got[0] for r in matched if (got := _agreement_pairs([r["per"]], keys))]
        # prospective = 登録日以降の発走 かつ **指数が発走前に生成されたレースのみ**
        # (発走後の再score は hindsight 汚染し得るので確証★の母集団に入れない)。
        pros_pairs = [got[0] for r in matched
                      if r["date"] and r["date"] >= rule["registered_at"]
                      and r["scored_pre_start"]
                      and (got := _agreement_pairs([r["per"]], keys))]
        mkt_pairs = [got[0] for r in matched if r["mper"] is not None
                     and (got := _agreement_pairs([r["mper"]], keys))]
        insample = _signal_stats(all_pairs, with_drop_best=True)
        prospective = _signal_stats(pros_pairs)
        status, status_label = _rule_status(prospective)
        rules_out.append({
            "key": rule["key"],
            "label": rule["label"],
            "strategy": rule["strategy"],
            "strategy_label": STRATEGY_SHORT_LABELS.get(rule["strategy"], rule["strategy"]),
            "condition_label": rule["condition_label"],
            "registered_at": rule["registered_at"],
            "discovery": rule.get("discovery") or "",
            "consensus": rule.get("consensus"),
            "style": rule.get("style"),
            "venue": rule.get("venue"),
            "min_runners": rule.get("min_runners"),
            "max_runners": rule.get("max_runners"),
            "skip_dead_cell": bool(rule.get("skip_dead_cell")),
            # 数値特徴量条件 (上位3頭ギャップ/荒れ具合)。frontend の per-race 発火判定
            # (betGuide.raceSignalRuleGuide) が同じ特徴量をミラー計算して評価する。
            "features": rule.get("features") or None,
            "insample": insample,
            "prospective": prospective,
            "market_baseline": {
                "races": len(mkt_pairs),
                "hits": sum(1 for p in mkt_pairs if p[1] > 0),
                "roi": _roi_of(mkt_pairs) if mkt_pairs else 0.0,
            },
            "status": status,
            "status_label": status_label,
        })

    # 表示順 (ユーザ指示 2026-07-05): 券種を 単勝1→単勝2→単勝3→複勝1→複勝2→複勝3→馬連1-2→
    # 馬連1-3→ワイド1-2→ワイド1-3 (それ以外は末尾) の順にグループ化し、各券種内は
    # 固定グリッド条件 (無条件/一致/荒れ) → 個別発見ルール の順。
    _order = {k: i for i, k in enumerate(_GRID_STRATEGIES)}
    rules_out = [r for _i, r in sorted(
        enumerate(rules_out),
        key=lambda t: (_order.get(t[1]["strategy"], 99),
                       0 if t[1]["key"].startswith("grid_") else 1, t[0]))]

    # 見送り規律の根拠: 死にセル (拮抗型 × 市場不一致) vs それ以外 のターゲット別 ROI。
    dead = [r for r in records if r["flags"]["style"] and not r["flags"]["consensus"]]
    alive = [r for r in records if not (r["flags"]["style"] and not r["flags"]["consensus"])]
    dead_targets = []
    for key, label, ks in _AGREEMENT_TARGETS:
        dp = _agreement_pairs([r["per"] for r in dead], ks)
        ap = _agreement_pairs([r["per"] for r in alive], ks)
        dead_targets.append({
            "key": key, "label": label,
            "dead_roi": _roi_of(dp) if dp else 0.0, "dead_races": len(dp),
            "alive_roi": _roi_of(ap) if ap else 0.0, "alive_races": len(ap),
        })

    walkforward = []
    for wf_key, wf_label, kw in (
        ("matrix_best", "マトリクス best セル追従 (セル n≥floor のみ賭ける)",
         {"floor": _MATRIX_SAMPLE_FLOOR, "fallback_overall": False}),
        ("matrix_best_fallback", "同上 + セル不足時は全体 best へフォールバック",
         {"floor": _MATRIX_SAMPLE_FLOOR, "fallback_overall": True}),
    ):
        stats = _walkforward_matrix(records, **kw)
        walkforward.append({"key": wf_key, "label": wf_label, **stats})

    last_updated_at: str | None = None
    for r in records:
        ts = r["recorded_at"]
        if isinstance(ts, str) and (last_updated_at is None or ts > last_updated_at):
            last_updated_at = ts
    return {
        "point_cost": point_cost,
        "races": len(records),
        "min_confirm": SIGNAL_RULE_MIN_CONFIRM,
        "min_broken": SIGNAL_RULE_MIN_BROKEN,
        "rules": rules_out,
        "dead_cell": {
            "label": "拮抗型 × 市場不一致 (死にセル)",
            "n": len(dead),
            "targets": dead_targets,
        },
        "walkforward": walkforward,
        "last_updated_at": last_updated_at,
        "sample_warning": len(records) < 50,
    }


def append_signal_rules_history() -> dict[str, Any] | None:
    """プレレジルールの現在値を history (jsonl) に追記 (結果取得ループから毎回呼ばれる)。

    `races` が前回 entry と同じなら no-op (新しい結果が無い)。確証★/破綻 への遷移が
    後から時系列で追えるよう、登録後 (prospective) 統計と状態だけを compact に残す。
    """
    cur = compute_signal_rules()
    if cur["races"] == 0:
        return None
    last_races = None
    if SIGNAL_RULES_HISTORY.exists():
        try:
            lines = SIGNAL_RULES_HISTORY.read_text(encoding="utf-8").splitlines()
            if lines:
                last_races = json.loads(lines[-1]).get("races")
        except (OSError, json.JSONDecodeError, ValueError):
            last_races = None
    if last_races == cur["races"]:
        return None
    import datetime as _dt
    row = {
        "recorded_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "races": cur["races"],
        "rules": [
            {"key": r["key"], "status": r["status"],
             "prospective": {
                 "races": r["prospective"]["races"],
                 "roi": round(r["prospective"]["roi"], 4),
                 "roi_ci_low": round(r["prospective"]["roi_ci_low"], 4),
                 "roi_ci_high": round(r["prospective"]["roi_ci_high"], 4),
             }}
            for r in cur["rules"]
        ],
        "walkforward": [
            {"key": w["key"], "races": w["races"], "roi": round(w["roi"], 4)}
            for w in cur["walkforward"]
        ],
    }
    SIGNAL_RULES_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_RULES_HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def signal_rules_history(limit: int = 200) -> list[dict[str, Any]]:
    """蓄積済のプレレジルール検証時系列 (古→新、最大 limit 件)。"""
    if not SIGNAL_RULES_HISTORY.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in SIGNAL_RULES_HISTORY.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-limit:]


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
