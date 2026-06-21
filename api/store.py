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
TIMELINE_DIR = ROOT / "data" / "cache" / "odds_timeline"
# 今日の勝負レース スキャン結果 (src/shobu.py が <date>.json を書く)。
SHOBU_DIR = ROOT / "data" / "cache" / "shobu"


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
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


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


def compute_shobu_pnl(point_cost: int = 100, box_size: int = 5) -> dict[str, Any]:
    """勝負レース (recommended) 専用の **仮想収支** (ユーザ指示 2026-06-21)。

    各勝負レースで Claude 指数 **上位 box_size 頭** (既定 5) の **3連単 BOX** を買ったと仮定し、
    実際の 1・2・3 着がその上位5頭の中に全て収まれば的中 (= BOX が当たる) として、その3連単
    配当 (trifecta_payout) で収支を集計する。「勝負レースだけの収支。上位5頭の内から 1,2,3着が
    いた場合的中。3連単を買っていたことにする」。

    - 対象: data/cache/shobu/<date>.json の recommended=true レースのうち、Claude 指数が
      ある (= 上位N頭を決められる) かつ 結果が確定している もの。
    - 上位N頭 = snapshot の Claude 指数 (index_compare / llm_win_index) 降順。N は
      `_shobu_box_size(出走頭数)` で決まる (≥8頭=5 / 7頭=4 / 少頭数は最低3頭を場外に残す)。
    - 3連単 BOX 点数 = P(N, 3) = 5×4×3 = 60点 (5頭) / 4×3×2 = 24点 (4頭)。
    - stake/race = 点数 × point_cost。payout = 的中時の trifecta_payout を point_cost(¥100単位)へ
      スケール。**長期回収を保証する指標ではなく「勝負レース判定 + 上位5頭 BOX」の paper 検証**。

    返り値は compute_calibration の bundle 集計と同じく hits/hit_rate/stake/payout/roi + CI、
    及び per-race detail (races_detail)。recommended だが Claude 指数なし=skipped_no_index、
    結果未確定=skipped_no_result でカウント (分母には入れない)。
    """
    from itertools import permutations

    # recommended レースを race_id で集約 (再スキャンの重複は generated_at 後勝ち。
    # race は1日1回なので race_id は実質一意だが、念のため最新スキャン結果を優先)。
    by_race: dict[str, dict[str, Any]] = {}
    if SHOBU_DIR.exists():
        for p in sorted(SHOBU_DIR.glob("*.json")):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            gen = doc.get("generated_at") or ""
            for race in doc.get("races") or []:
                if not race.get("recommended"):
                    continue
                rid = race.get("race_id")
                if not rid:
                    continue
                prev = by_race.get(rid)
                if prev is None or gen >= (prev.get("_generated_at") or ""):
                    by_race[rid] = {**race, "_generated_at": gen}

    races_detail: list[dict[str, Any]] = []
    per_race: list[tuple[int, int]] = []
    hits = 0
    stake_sum = 0
    payout_sum = 0
    skipped_no_index = 0
    skipped_no_result = 0
    last_updated_at: str | None = None

    for rid, race in by_race.items():
        safe = _safe_race_id(rid)
        if safe is None:
            continue
        snap_path = PRED_DIR / f"{safe}.json"
        if not snap_path.exists():
            skipped_no_index += 1
            continue
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            skipped_no_index += 1
            continue
        idx = _claude_index_by_number(snap)
        if len(idx) < 3:
            skipped_no_index += 1   # Claude 指数が 3頭未満 = 3連単 BOX を組めない
            continue
        # 出走頭数 (頭立て) で BOX サイズを決める (7頭立て→4頭BOX 等)。
        # 権威値は snapshot の n_runners、無ければ shobu entry → Claude 指数頭数。
        n_runners = snap.get("n_runners") or race.get("n_runners") or len(idx)
        box = _shobu_box_size(n_runners, base=box_size)
        top = [num for num, _ci in
               sorted(idx.items(), key=lambda kv: kv[1], reverse=True)][:box]
        top_set = set(top)

        result_path = RESULT_DIR / f"{safe}.json"
        if not result_path.exists():
            skipped_no_result += 1
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            skipped_no_result += 1
            continue
        finish = [x for x in (result.get("finish_order") or [])[:3]
                  if isinstance(x, int) and x > 0]
        if len(finish) < 3:
            skipped_no_result += 1   # 着順が3着まで確定していない (placeholder/壊れ)
            continue

        n_points = len(list(permutations(top, 3)))     # = P(len(top), 3) = 60 (5頭)
        stake = n_points * point_cost
        hit = all(f in top_set for f in finish)
        tri = int(result.get("trifecta_payout") or 0)
        # 3連単配当は ¥100 単位 → point_cost にスケール (point_cost=100 なら配当そのまま)。
        payout = int(round(tri * point_cost / 100.0)) if hit else 0

        hits += 1 if hit else 0
        stake_sum += stake
        payout_sum += payout
        per_race.append((stake, payout))
        r_ts = result.get("recorded_at")
        if isinstance(r_ts, str) and (last_updated_at is None or r_ts > last_updated_at):
            last_updated_at = r_ts

        races_detail.append({
            "race_id": rid,
            "date": (race.get("_generated_at") or "")[:10],
            "venue": race.get("venue") or "",
            "race_no": race.get("race_no"),
            "race_type": race.get("race_type"),
            "shobu_score": race.get("shobu_score"),
            "matched": race.get("matched") or [],
            "n_runners": n_runners,       # 出走頭数 (頭立て)
            "box": len(top),              # BOX に使った上位頭数 (7頭立て=4 等)
            "top_horses": top,            # Claude 指数上位N頭 (馬番)
            "finish": finish,             # 実 1-2-3着
            "n_points": n_points,         # 3連単 BOX 点数 (P(box,3))
            "stake": stake,
            "hit": hit,
            "payout": payout,
            "trifecta_payout": tri,
            "saved_at": snap.get("saved_at"),
        })

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
        "recommended_total": len(by_race),  # 勝負レース総数 (指数/結果欠落も含む)
        "skipped_no_index": skipped_no_index,
        "skipped_no_result": skipped_no_result,
        "last_updated_at": last_updated_at,
        "sample_warning": n < 30,
        "races_detail": races_detail,
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
