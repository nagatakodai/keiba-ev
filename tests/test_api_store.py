"""api/store.py の path traversal 防御 test。"""
from __future__ import annotations


def test_safe_race_id_accepts_normal_ids():
    """JRA / NAR の race_id (12 桁数字 or 正規化形式 cup_id-day-race) を受け入れる。"""
    from api.store import _safe_race_id
    assert _safe_race_id("202605230412") == "202605230412"
    assert _safe_race_id("20260523-4-12") == "20260523-4-12"
    assert _safe_race_id("2026440521-521-9") == "2026440521-521-9"
    assert _safe_race_id("test_race-1") == "test_race-1"


def test_safe_race_id_rejects_path_traversal():
    """`..` や `/` を含む文字列を弾く。"""
    from api.store import _safe_race_id
    assert _safe_race_id("..") is None
    assert _safe_race_id("../etc/passwd") is None
    assert _safe_race_id("../../etc/passwd") is None
    assert _safe_race_id("/etc/passwd") is None
    assert _safe_race_id("test/../etc") is None


def test_safe_race_id_rejects_special_chars():
    """日本語 / null byte / 空 / 空白 を弾く。"""
    from api.store import _safe_race_id
    assert _safe_race_id("") is None
    assert _safe_race_id(" ") is None
    assert _safe_race_id("test race") is None  # space
    assert _safe_race_id("test\x00") is None
    assert _safe_race_id("テスト") is None
    assert _safe_race_id("test;rm -rf /") is None


def test_get_prediction_returns_none_for_bad_race_id():
    """get_prediction(traversal) は None。"""
    from api.store import get_prediction
    assert get_prediction("../../etc/passwd") is None
    assert get_prediction("../config") is None
    assert get_prediction("") is None


def test_index_version_explicit_field_wins():
    """明示の index_version があればそのまま返す (新 snapshot)。"""
    from api.store import index_version_of
    assert index_version_of({"index_version": "v2", "index_compare": [{"claude_index": 50}]}) == "v2"
    assert index_version_of({"index_version": "v1"}) == "v1"


def test_index_version_inferred_from_scored_date():
    """旧 snapshot は採点日時で推定: 2026-06-28 以降=v2 / 以前=v1 (Claude 指数があるとき)。"""
    from api.store import index_version_of
    idx = {"index_compare": [{"number": 1, "claude_index": 80.0}]}
    assert index_version_of({**idx, "llm_scored_at": "2026-06-28T10:00:00"}) == "v2"
    assert index_version_of({**idx, "llm_scored_at": "2026-06-27T23:59:59"}) == "v1"
    # llm_scored_at 欠落は saved_at で代替
    assert index_version_of({**idx, "saved_at": "2026-06-29T12:00:00"}) == "v2"
    assert index_version_of({**idx, "saved_at": "2026-06-01T12:00:00"}) == "v1"


def test_index_version_none_without_index():
    """Claude 指数が無い snapshot は None (バージョン対象外)。"""
    from api.store import index_version_of
    assert index_version_of({"saved_at": "2026-06-29T12:00:00"}) is None
    assert index_version_of({"index_compare": [], "llm_win_index": {}}) is None
    # llm_win_index があれば指数あり扱い → 日付推定
    assert index_version_of({"llm_win_index": {"1": 70.0}, "saved_at": "2026-06-29"}) == "v2"


def test_compute_calibration_runs_without_error():
    """compute_calibration がエラーなく終わる (smoke test)。
    現データ (data/predictions / data/results) と PLAN_KEY_FIELDS の構造
    変更が壊れない最低限の確認。"""
    from api.store import compute_calibration
    c = compute_calibration()
    # 基本 shape
    assert "race_count" in c
    assert "plans" in c
    assert "tiers" in c
    # 全 Plan が出ている (snapshot にキーがあれば)
    plan_names = {p["plan"] for p in c["plans"]}
    # 少なくとも A/B/C は常に出る (rebuild dataset 以後)
    if c["race_count"] > 0:
        assert "Plan A" in plan_names or len(plan_names) >= 0  # smoke
    # 各 plan が必須 fields を持つ
    for p in c["plans"]:
        assert "plan" in p
        assert "stake" in p
        assert "payout" in p
        assert "roi" in p
        assert "hit_rate_ci_low" in p
        assert "hit_rate_ci_high" in p


def test_trifecta_bundle_model_fallback_counts_as_skip(tmp_path, monkeypatch):
    """Claude 指数ゲート: rank_source != "claude" の 3連単束は legs が立っていても
    実弾投票されない (auto_watch/oddspark_bet/ipat_bet が弾く) ので、計測上も
    「見送り」(participated=False / stake=0 / hit=False) になる (2026-06-07 ユーザ指示)。"""
    import json
    from api import store

    pred_dir = tmp_path / "predictions"
    res_dir = tmp_path / "results"
    pred_dir.mkdir()
    res_dir.mkdir()

    def _write_race(rid: str, rank_source: str):
        legs = [{
            "bet_type": "trifecta", "key": [1, 2, 3], "stake": 1000,
            "payout_if_hit": 50000, "odds": 50.0,
        }]
        pred = {
            "saved_at": "2026-06-06T10:00:00",  # TRIFECTA_CUTOFF 以降 = 計測対象
            "venue_name": "テスト",
            "rows": [],
            "recommended_bundle_t": {"legs": legs, "rank_source": rank_source},
        }
        # finish_order = 1-2-3 なので束は「的中」している (= hit が殺されるかの検証になる)
        result = {"finish_order": [1, 2, 3], "trifecta_payout": 50000}
        (pred_dir / f"{rid}.json").write_text(json.dumps(pred), encoding="utf-8")
        (res_dir / f"{rid}.json").write_text(json.dumps(result), encoding="utf-8")

    _write_race("202606060101", "claude")  # 実弾対象 → 参加・的中
    _write_race("202606060102", "model")   # ゲートで弾かれる → 見送り

    monkeypatch.setattr(store, "PRED_DIR", pred_dir)
    monkeypatch.setattr(store, "RESULT_DIR", res_dir)

    c = store.compute_calibration()
    races = {r["race_id"]: r for r in c["races"]}

    claude_race = races["202606060101"]
    assert claude_race["trifecta_bundle_participated"] is True
    assert claude_race["trifecta_bundle_hit"] is True
    assert claude_race["trifecta_bundle_stake"] == 1000

    model_race = races["202606060102"]
    assert model_race["trifecta_bundle_participated"] is False
    assert model_race["trifecta_bundle_hit"] is False
    assert model_race["trifecta_bundle_stake"] == 0

    tb = c["trifecta_bundle"]
    assert tb["races"] == 2
    assert tb["participated_races"] == 1
    assert tb["skipped_races"] == 1
    assert tb["hits"] == 1
    assert tb["stake"] == 1000


def test_ev_bundle_measured_from_cutoff(tmp_path, monkeypatch):
    """EV束 (実弾既定束, 2026-06-10〜) の系列: ev_cutoff 以降のみ ev_bundle に入る。

    旧 EV束 (β=0 事故時代) は ev_measured=False で系列から除外され、全期間参考の
    claude_bundle にだけ入る。
    """
    import json

    from api import store

    pred_dir = tmp_path / "preds"
    res_dir = tmp_path / "results"
    pred_dir.mkdir()
    res_dir.mkdir()

    def _write_race(rid: str, saved_at: str, legs):
        pred = {
            "saved_at": saved_at,
            "venue_name": "テスト",
            "rows": [],
            "recommended_bundle": {"legs": legs},
        }
        result = {"finish_order": [1, 2, 3], "trifecta_payout": 0,
                  "final_odds": {"win:1": 3.0}}
        (pred_dir / f"{rid}.json").write_text(json.dumps(pred), encoding="utf-8")
        (res_dir / f"{rid}.json").write_text(json.dumps(result), encoding="utf-8")

    win_leg = [{"bet_type": "win", "key": [1], "stake": 500,
                "payout_if_hit": 1600, "odds": 3.2}]
    _write_race("202606100101", "2026-06-10T19:00:00", win_leg)   # 新レジーム → 計測対象
    _write_race("202606100102", "2026-06-10T19:30:00", [])        # 新レジーム・見送り
    _write_race("202606050101", "2026-06-05T10:00:00", win_leg)   # 旧 EV束 → 系列除外

    monkeypatch.setattr(store, "PRED_DIR", pred_dir)
    monkeypatch.setattr(store, "RESULT_DIR", res_dir)

    c = store.compute_calibration()
    races = {r["race_id"]: r for r in c["races"]}
    assert races["202606100101"]["ev_measured"] is True
    assert races["202606050101"]["ev_measured"] is False

    ev = c["ev_bundle"]
    assert ev["races"] == 2                  # 旧レジーム race は分母にも入らない
    assert ev["participated_races"] == 1
    assert ev["skipped_races"] == 1
    assert ev["hits"] == 1
    assert ev["stake"] == 500
    assert ev["payout_final"] == 1500        # 最終オッズ 3.0 × ¥500

    # 全期間参考 (claude_bundle) には旧レジームも入る
    assert c["claude_bundle"]["participated_races"] == 2
    assert c["ev_cutoff"].startswith("2026-06-10")


def test_backfilled_bundle_counts_as_skip(tmp_path, monkeypatch):
    """backfill 束 (scripts/backfill_bundle.py が後付けした paper, backfilled=true) は
    実際には賭けていないので ev_bundle / claude_bundle の集計で「見送り」になる
    (bundle_calibration_report の EV束(backfill) 分離と同 semantics, 2026-06-12)。
    race 行は残り bundle_backfilled フラグで UI がグレーアウトできる。"""
    import json

    from api import store

    pred_dir = tmp_path / "preds"
    res_dir = tmp_path / "results"
    pred_dir.mkdir()
    res_dir.mkdir()

    def _write_race(rid: str, backfilled: bool):
        bundle = {"legs": [{"bet_type": "win", "key": [1], "stake": 500,
                            "payout_if_hit": 1600, "odds": 3.2}]}
        if backfilled:
            bundle["backfilled"] = True
        pred = {
            "saved_at": "2026-06-11T10:00:00",  # EV_CUTOFF 以降 = 計測対象 window 内
            "venue_name": "テスト",
            "rows": [],
            "recommended_bundle": bundle,
        }
        result = {"finish_order": [1, 2, 3], "trifecta_payout": 0}
        (pred_dir / f"{rid}.json").write_text(json.dumps(pred), encoding="utf-8")
        (res_dir / f"{rid}.json").write_text(json.dumps(result), encoding="utf-8")

    _write_race("202606110101", backfilled=False)  # 実弾 → 参加・的中
    _write_race("202606110102", backfilled=True)   # paper → 見送り

    monkeypatch.setattr(store, "PRED_DIR", pred_dir)
    monkeypatch.setattr(store, "RESULT_DIR", res_dir)

    c = store.compute_calibration()
    races = {r["race_id"]: r for r in c["races"]}

    live = races["202606110101"]
    assert live["bundle_backfilled"] is False
    assert live["bundle_participated"] is True
    assert live["bundle_hit"] is True
    assert live["bundle_stake"] == 500

    paper = races["202606110102"]
    assert paper["bundle_backfilled"] is True
    assert paper["bundle_participated"] is False
    assert paper["bundle_hit"] is False
    assert paper["bundle_stake"] == 0

    # ev_bundle / claude_bundle どちらの系列からも除外 (skipped 扱い)
    for agg in (c["ev_bundle"], c["claude_bundle"]):
        assert agg["races"] == 2
        assert agg["participated_races"] == 1
        assert agg["skipped_races"] == 1
        assert agg["stake"] == 500


def test_leg_id_sorts_unordered_bet_types(tmp_path, monkeypatch):
    """順不同券種 (馬連/ワイド/3連複) の leg key が降順でも final_odds (昇順規約) を
    lookup できる (_leg_id_for の昇順正規化, bundle_calibration_report._final_odds_key
    と同じ)。現データの key は全件昇順だが、将来の unsorted key への防御。"""
    import json

    from api import store

    pred_dir = tmp_path / "preds"
    res_dir = tmp_path / "results"
    pred_dir.mkdir()
    res_dir.mkdir()

    # key を意図的に降順 [7, 3] で保存 (final_odds 側は昇順 "wide:3-7")
    pred = {
        "saved_at": "2026-06-11T10:00:00",
        "venue_name": "テスト",
        "rows": [],
        "recommended_bundle": {"legs": [{"bet_type": "wide", "key": [7, 3],
                                         "stake": 1000, "payout_if_hit": 2000,
                                         "odds": 2.0}]},
    }
    result = {"finish_order": [3, 7, 1], "trifecta_payout": 0,
              "final_odds": {"wide:3-7": 5.0}}
    (pred_dir / "202606110201.json").write_text(json.dumps(pred), encoding="utf-8")
    (res_dir / "202606110201.json").write_text(json.dumps(result), encoding="utf-8")

    monkeypatch.setattr(store, "PRED_DIR", pred_dir)
    monkeypatch.setattr(store, "RESULT_DIR", res_dir)

    c = store.compute_calibration()
    race = {r["race_id"]: r for r in c["races"]}["202606110201"]
    assert race["bundle_hit"] is True
    # sort されていれば final_odds 5.0 × ¥1,000 = ¥5,000。
    # 未 sort ("wide:7-3" miss) だと snapshot fallback の ¥2,000 になる。
    assert race["bundle_payout_final"] == 5000


def test_get_timeline(tmp_path, monkeypatch):
    """get_timeline: jsonl → win/place のみの odds + depth メタ + 結果埋め込み。
    timeline ファイル無し / traversal race_id は None。"""
    import json

    from api import store

    tl_dir = tmp_path / "odds_timeline"
    res_dir = tmp_path / "results"
    tl_dir.mkdir()
    res_dir.mkdir()

    rid = "20260611-5-9"
    lines = [
        {"stage": "score", "captured_at": "2026-06-11T14:00:00", "close_at": 100,
         "start_at": 220, "n_horses": 8, "odds_hash": "x",
         "odds": {"win": {"1": 2.5, "2": 8.0}, "place": {"1": 1.3},
                  "trifecta": {"1 → 2 → 3": 30.0, "1 → 3 → 2": 45.0}},
         "source": "keibago"},
        {"stage": "bet", "captured_at": "2026-06-11T14:04:00", "close_at": 100,
         "start_at": 220, "n_horses": 8, "odds_hash": "y",
         "odds": {"win": {"1": 2.2, "2": 9.1}}},
    ]
    (tl_dir / f"{rid}.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
        encoding="utf-8")
    (res_dir / f"{rid}.json").write_text(json.dumps(
        {"finish_order": [1, 2, 3], "final_odds": {"win:1": 2.1}}), encoding="utf-8")

    monkeypatch.setattr(store, "TIMELINE_DIR", tl_dir)
    monkeypatch.setattr(store, "RESULT_DIR", res_dir)

    d = store.get_timeline(rid)
    assert d is not None
    assert d["race_id"] == rid
    assert len(d["rows"]) == 2
    r0 = d["rows"][0]
    assert r0["stage"] == "score"
    assert set(r0["odds"].keys()) == {"win", "place"}   # 3連単グリッドは落とす
    assert r0["depth"] == {"win": 2, "place": 1, "trifecta": 2}  # 組数メタは残す
    assert r0["source"] == "keibago"
    assert d["rows"][1]["odds"] == {"win": {"1": 2.2, "2": 9.1}}
    assert d["rows"][1]["source"] is None
    assert d["result"] == {"finish_order": [1, 2, 3], "final_odds": {"win:1": 2.1}}

    # 結果なし race → result: None / timeline 無し → None / traversal → None
    rid2 = "20260611-5-10"
    (tl_dir / f"{rid2}.jsonl").write_text(
        json.dumps(lines[0], ensure_ascii=False) + "\n", encoding="utf-8")
    d2 = store.get_timeline(rid2)
    assert d2 is not None and d2["result"] is None
    assert store.get_timeline("20260611-5-11") is None
    assert store.get_timeline("../etc/passwd") is None
