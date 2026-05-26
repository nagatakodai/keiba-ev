# keiba-ev — 中央競馬 EV 分析プロジェクト

## netkeiba IP 規制対応

netkeiba は `race.netkeiba.com` と `nar.netkeiba.com` の特定 IP に対し、短時間で大量 request すると **CloudFront 経由で HTTP 400 を返す** (= empty body `<html><head></head><body></body></html>`)。本リポジトリでは:

- `src.scrape.fetch_html` が空 HTML を検出して `NetkeibaBlocked` 例外を投げる
- `src.bulk_fetch` および `scripts/fetch_trifecta_odds_holdout.py` は空 HTML を保存しない (= ゴミファイルを作らない)
- `src.auto_watch` が両ドメイン block 検出時に明確なエラーメッセージを出す

**block 中の運用:**
1. **数時間〜1日待つ** — netkeiba の rate-limit は時間で解除される
2. **VPN / 別 IP** — 別ネットから繋ぐと即解除されることが多い
3. **オフライン解析は継続可能** — `data/raw/` のキャッシュ済 HTML、`data/datasets/all.parquet`、`data/cache/` (trifecta odds / aptitudes) は全て手元にある:
   - `make holdout` / `cv_*` / `sliding_window_eval` は全て scrape 不要
   - 過去 race の `python -m src.analyze --html data/raw/<rid>-shutuba.html.gz` も可能
   - LLM 再評価 (`claude -p`) は web search だけなので netkeiba block 関係なし
4. **代替サイト (`src/scrape_alt.py`)** — race list 用途のみ:
   - **keibalab.jp** が race_id 形式同じ + JS 不要 + 当日 race list 提供
   - `fetch_race_list_keibalab()` で当日 race_id を列挙可能
   - 但し **fresh odds の代替は無い** (keibalab の odds page は JS render 必須かつ
     3 連単 per-jiku 単位を提供しない、JRA 公式は Shift_JIS の interactive form POST)
   - つまり「block 中の watch-auto による race discovery」のみ救える、live betting は不可

| サイト | 状態 | 用途 |
|---|---|---|
| race.netkeiba.com / nar.netkeiba.com / db.netkeiba.com | ❌ block 中 | 全データ (本命) |
| www.netkeiba.com | ✅ 200 | ニュース・記事のみ |
| **keibalab.jp** | ✅ 200 | race list / 出馬表 (fallback)。※当日一覧は JS 化で discovery 不安定、NAR 弱 |
| **keiba.go.jp (地方競馬公式)** | ✅ 200 | **NAR オッズの第一フォールバック** (`src/scrape_keibago.py`)。**全6券種 (単複/馬連/ワイド/馬単/3連複/3連単) を組合せ明示・静的UTF-8 HTML・GET・会員不要** で取得。位置推定不要なので誤オッズが原理的に出ない (実機: ワイド≤馬連 55/55, 完全列挙)。oddspark の上位互換。当日レース向け (TodayRaceInfo) |
| **oddspark.com** | ✅ 200 | NAR オッズ二次 fallback (`src/scrape_oddspark.py`)。**単勝/複勝/3連単 のみ採用**。馬連/ワイド/馬単/3連複 は位置推定パースが誤オッズを出すため無効化 (下記)。keiba.go.jp が解決できない場のみ使用 |
| www.jra.go.jp | ✅ 200 | Shift_JIS + form POST (`accessO.html` CNAME チェーン)、scrape 難、JRA のみ。JRA 開催日に明示形式の要実機検証 |
| sports.yahoo.co.jp/keiba | ✅ 200 | JS render 必須 |

**NAR オッズ fallback (`src/scrape_oddspark.py`)**: netkeiba block 中、NAR レースの **単勝/複勝/3連単** を oddspark から取得して解析継続できる。Playwright 不要、すべて HTTP GET。`python -m src.scrape_oddspark <netkeiba_nar_race_id>` で EV + トリガミ防止済の総合オススメを出力。

**重要 (cross-validation の教訓)**: bet type の検証は **組合せ数だけでなく netkeiba 実オッズとの照合**が必須。当初は count だけ見て「全 7 type 対応」としたが、netkeiba snapshot の実オッズと照合した結果:
- **採用 (照合 OK)**: 単複 (list で 馬番+馬名+オッズ明示) → 単勝 6/8 一致 / 3連単 (`th` に "a → b → c" 明示) → ~85% 一致 (残差は最終 vs 締切5分前のドリフト)。**組合せが HTML に明示**されるため信頼できる。
- **無効化 (誤オッズ)**: 馬連/ワイド/馬単/3連複 のグリッドは 1 セルに馬番が片方しか出ず、もう片方を **列位置から推定**する。>9 頭でセル折り返しが起きると誤った組にオッズが付き、12 頭の佐賀 R12 で **1番人気 (6,7) 2.4 倍 → 514 倍** の取り違えを確認。誤オッズは賭け金が動く最悪のバグなので production では無効 (`fetch_oddspark_bets`)。parser 関数 (`parse_pair_grid`/`parse_exacta_grid`/`parse_trio_grid`) は将来の信頼できる解法用に残置・未使用。
- → fallback は **単勝 (唯一 robust な +EV 戦略) + 3連単 (総合オススメの主役)** をカバー。JRA は oddspark 非対応 → VPN/別IP。出馬表/馬柱は `data/raw` の netkeiba cache があれば使い(確率モデルが効く)、無ければ oddspark の馬リスト (`selectHorseNb` option = 全出走馬の権威ソース) + 単勝市場率主導 (`estimate_probs(market_win_override=...)`)。oddspark の場コード (opTrackCd) は netkeiba と別 namespace なので **場名でマッチング**。

oddspark のオッズ表構造 (採用 = 組合せが HTML に明示・実オッズ照合 OK):
- **単複** (betType=1): list `[枠, 馬番, 馬名, 単勝, 複勝min-max]` → ✅ 採用 (複勝は下限採用)。
- **3連単** (8): list `<th class="th2">a → b → c</th>` (組合せ明示)。1着軸は **`&horseNb=N` GET で切替** (JS `url += "&horseNb=" + selectHorseNb.val()`) → 全軸巡回で **全 N(N-1)(N-2) 点列挙** (`fetch_oddspark_trifecta`)。✅ 採用。
- **馬連/ワイド/馬単/3連複** (6/7/5/9): グリッドで 1 セルに馬番が **片方のみ**、他方を列位置から推定 → >9 頭の折り返しで誤オッズ (実オッズ照合で確認)。**無効化** (parser は残置・未使用)。
- JRA は oddspark 非対応 → VPN/別IP 推奨

**馬柱 (past_runs) も oddspark から取得**: cache に netkeiba 出馬表が無い当日新規 NAR レースでも、`HorseDetail.do?lineageNb=<id>` の成績表をパース (`parse_horse_detail`) して各馬の `past_runs` を構築 → `build_features` が効き、確率が市場主導でなく**モデルの edge を反映**する。lineageNb は単複ページの `HorseDetail` リンクから馬番別に取得。タイム列は馬の自走時計なので own_time_sec に直接採用、着順は netkeiba 馬柱と同じく 1/2/3 のみ int・他は None。1 レース当たり頭数分の追加 fetch (~15-20s)。
  - **leakage 防止 (重要)**: HorseDetail は「過去 race を解析する」と対象 race 自身の結果も履歴に含む (= 予測対象の着順が特徴量に漏れる)。`build_oddspark_racedata` は **対象 race 日付以降の run を除外** + **直近5走に制限** (netkeiba 馬柱の窓に合わせる)。live (発走前) では対象 race は未走なので no-op。
  - **整合検証**: cached 馬柱がある race で netkeiba 由来 speed_idx と oddspark 由来 speed_idx を照合 → leakage 修正後は **~2% 以内で一致** (修正前は ~12% 系統ズレ)。これでモデルの学習分布 (netkeiba 馬柱) と oddspark 馬柱が整合する。

**NAR オッズ第一源 = keiba.go.jp (`src/scrape_keibago.py`)**: netkeiba は NAR の馬連/ワイドが壊れ (上記)、oddspark はグリッド誤オッズで馬連/ワイド/馬単/3連複が無効。**keiba.go.jp は全6券種を組合せ明示で取れる**ので、誤オッズ無しで馬連/ワイド/馬単/3連複/3連単すべてを復活できる。`find_keibago_race` は netkeiba NAR race_id → 場名(`VENUE_CODE`) → babaCode を `TodayRaceInfoTop` から**動的照合**(別 namespace の babaCode 誤りで別場を取らない安全策)。`analyze_keibago` は cache 出馬表 (馬柱) があれば確率モデルを効かせ、無ければ単複の馬リスト+市場ブレンド主導。**安全ゲート**: `check_consistency` で「ワイド>馬連」等を検知したら pair/trio 系を drop (誤オッズより見送り)。`python -m src.scrape_keibago <netkeiba_nar_rid> [--snapshot]`。TodayRaceInfo なので当日向け。

**watch-auto への統合 (block 中も自動継続)**: netkeiba 両ドメイン block 時、`auto_watch._list_due_races` は **oddspark で NAR race discovery** (`fetch_race_list_oddspark`: KaisaiRaceList→OneDayRaceList で当日全 NAR の race_id + 発走時刻) に fallback し、該当レースは `source="oddspark"` を付けて `_dispatch_nar_fallback` で dispatch する → **keiba.go.jp を優先** (`_dispatch_keibago`: `python -m src.scrape_keibago <rid> --snapshot --start-at=<unix>`、全6券種)、keiba.go.jp が解決できない場のみ **oddspark にフォールバック** (`_dispatch_oddspark`、単複/3連単)。トリガミ防止束を含む snapshot を保存 (`odds_source="keibago"`/`"oddspark"`, 発走時刻も補完)。これで規制中でも NAR の watch-auto が止まらず EV picks を出し続ける (keibalab は当日一覧が JS 化で discovery 不能なため最終 fallback)。claude 調査 (3連単 plan の検索補強 + 総合オススメ束の web 検証) も netkeiba 経路と同じ関数で実行され、履歴・snapshot も同形式で作られる。

**block 中の result fetch (`fetch_result.process_pending`)**: 結果取得は netkeiba 依存 (block 中は失敗) だが、失敗理由が block (`NetkeibaBlocked`/空 body/CloudFront) のときは **attempt を消費せず `BLOCK_RETRY_INTERVAL_SEC` (15分) 間隔で pending を維持**する。これで block 中に走った race を terminal `failed` にして取りこぼさず、解除後に結果取得 → calibration に反映できる (block でない通常の失敗は従来どおり max_attempts で failed)。

**保存基準 (GCS/BigQuery 移行)** — 現状 raw HTML ~2.3GB / parquet <1MB なのでローカル維持で十分。**数百 GB を超える前に GCS bucket + BigQuery テーブルに移行**する想定 (新規スクリプトを書いて parquet を bq load → SQL で集計、raw HTML は GCS にミラー)。



このリポジトリは、中央競馬 (JRA) の 3 連単について **EV (期待値) > 1** の買い目を netkeiba の実オッズから抽出するためのツール群を提供する。Claude (または人間) がこのリポジトリで作業するときは、以下の流儀を厳守すること。

## 目的と前提

- 予算 **¥10,000** で 3 連単の EV > 1 を狙う。
- 単発レースの勝敗は EV と直接相関しない。**長期試行で初めて意味を持つ**。
- 「EV ≤ 1」のレースは**打たない**(スキップ推奨) 勇気を持つ。
- リアルタイムオッズを推測値で代用しない。取れないときは「リアルタイム取得不可」と明示。

## EV の定義

```
EV (回収率) = 的中率 × 平均オッズ ÷ 点数
```

- 中央競馬の 3 連単控除率は約 22.5%、市場効率では `P × O ≒ 0.775`。
- `P × O > 1.0` で理論上 +EV だが、本リポジトリの確率モデルは粗い heuristic で **楽観バイアス** がある。実運用の **Plan 入りフロアは P × O ≥ 1.02** に引き上げる。
- **点数で割らないと意味がない**。「想定的中率 × 想定オッズ = EV」のテンプレ計算には騙されない。

## パイプライン構成 (Phase 5 以降)

**EV だけでなく競馬独自の当て方も使う。EV は最終フィルタ。**

1. **適性指数 (`src/aptitude.py`)**: 各馬の 9 因子 (能力 / 距離適性 / 末脚 / コース / 馬場 / 状態 / 騎手 / ペース fit / 重賞実績) を 0-100 でレース内正規化。総合は重み付け平均。
2. **確率モデル (`src/ev.py:estimate_probs`)**: Layer 1 特徴量 + 市場ブレンド + Discounted Harville で win/place2/place3 確率を出す。Plackett-Luce 連鎖で 3 連単・3 連複・馬連・ワイド・馬単・単勝・複勝 すべての確率を導出。
3. **複数 bet type の EV**: 単勝 / 複勝 / 3 連単 を同じ確率モデルで EV table 化。控除率の低い bet type は +EV が残りやすいが、**馬連/ワイド/馬単/3連複 は実オッズが取れないため無効**:
   - **netkeiba**: `odds_get_form.html` の b3 (馬連) / b4 (ワイド) は jiku 巡回しても**実オッズでない合成/不完全値**を返す (実機確認: 12頭 NAR で ワイド>馬連 20/26 ペア、馬連<単勝 9/27、ワイドが 918.0/k の機械的パターン)。`fetch_and_parse(with_pair_bets=False)` で既定無効 (`誤オッズは賭け金が動く最悪のバグ`)。
   - **oddspark**: グリッドの位置推定で誤オッズ (別記)。
   - → 信頼できるのは **単複 (b1, 単一馬明示) + 3連単 (b8, 組合せ明示)** のみ。実オッズ照合に通る解法が確立したら復活させる。
4. **Plan G (適性ゲート → EV 足切り)**: 適性総合 top N 頭 (デフォルト 6) の集合内で生成される買い目のみ → P×O ≥ 1.02 で足切り。EV-first の Plan A/B/C と並列で提案される、競馬独自の「適性で選んで EV で確認」戦略。
5. **検索 MCP 補強**: LLM (`claude -p`) が適性指数 + Plan G を受け取って、検索で根拠を検証 / 補強根拠数で再ランク。

snapshot に保存される主要フィールド:
- `horse_aptitude`: 各馬の指数 + 内訳 (total 降順)
- `aptitude_top_horses`: Plan G の集合
- `plan_a_keys` / `plan_b_keys` / `plan_c_keys` / `plan_g_keys` / `plan_h1_keys` / `plan_h2_keys` / `plan_f_keys` (3 連単)
- `bet_tables`: 単勝 / 複勝 の EV top 30 (馬連/ワイド/馬単/3連複 は実オッズが取れず無効 = 空。`build_all_bet_tables` は `rd.other_bets` の非空 type のみ出す)
- `bet_tables_g`: 各 bet type の Plan G picks
- `recommended_bundle`: 「Claude 総合オススメ」= 全 bet type 横断の **joint (同時) Kelly 最適まとめ買い束** (`src/portfolio.py`)。レースの完全な top-3 結果分布 (全 ordered triple, Σp=1) 上で束全体の E[log(資金)] を最大化した成長率最適配分。独立 Kelly の単純和ではなく相関・排他性を考慮。+EV (P×O≥1.02) が無ければ legs 空 = 見送り。
  - **トリガミ防止 (安全マージン付き)**: `odds×stake < 投資総額 × TORIGAMI_MARGIN` の脚を除去 → 残脚で再最適化を収束まで繰り返す。`min_payout_ratio ≥ TORIGAMI_MARGIN` を保証。**margin=1.10** (`src/portfolio.py`) は「束を組んだ時点のオッズ」からの**下振れ緩衝**: 締切直前ドリフトや複勝のレンジ幅で実払戻が下振れしても、~9% までは収支マイナスにならない (margin=1 では保存オッズでしかトリガミ無を保証できず、実オッズ乖離でトリガミ化していた)。`dropped_torigami` に除外数、`torigami_margin` も snapshot に保存。
    - **レンジ型 bet の下限採用**: 複勝は `fuku_min` (下限) を採用 (実払戻 ≥ 下限で確定 → トリガミ保証が崩れない)。これと margin の二段構えで「オッズ乖離 → トリガミ」を防ぐ。束に乗るのは現状 単勝/複勝/3連単 のみ (馬連/ワイド等は誤オッズで無効、上記参照)。
  - **claude -p 検証** (`llm.validate_bundle_stream`, `analyze._validate_and_update_bundle`): 束の絡む馬を web 検索で裏取り (取消/体調/適性) し、明確なマイナス根拠のある脚を cut → 再構築。`llm_review` (summary/confidence/cuts/notes) を添えて recommended_bundle を上書き。`--no-llm` 時はモデルのみ。**束はまずモデルのみで生成・保存され、その後 claude -p が検証する**ので、検証前は frontend で「総合オススメ (モデル)」+「Claude 検証前」バッジ表示とし、検証済 (`llm_review.validated`) で初めて「Claude 総合オススメ」+ magenta バッジに切替 (検証前に Claude が裏取りしたかのような誤認を防ぐ)。
  - frontend (履歴詳細ページ最上部) は full Kelly を表示しつつ ½ Kelly を実運用推奨として併記 (楽観バイアス対策)、的中時払戻・min_payout_ratio (目標 ≥×margin で色分け)・検証バッジ/調査メモも表示。古い snapshot は欠落 → 近似 Kelly ランキングに fallback。`scripts/backfill_bundle.py` で後付け (start_at/close_at も再パース補正)

## 確率モデルの保守化 (このプロジェクトで最も重要)

EV の絶対値を「現実の的中率」に近づけることが、長期回収率底上げの **根幹**。
EV 3.0 と表示されても、それが「確率モデルの楽観バイアスで膨れただけ」なら長期では負ける。
逆に EV 1.10 でも、確率推定が現実と一致していれば **必ず黒字**。
ゆえに本プロジェクトでは「EV の数字を膨らませる」より「EV の数字を現実に合わせる」が常に優先される。

### 楽観バイアスの源 (`src/ev.py` の `estimate_probs`)

1. **レーティング線形補正**: `rp_factor = max(0.3, 1.0 + (rating - mean) / mean * 0.6)`
   - レーティング (netkeiba のタイム指数等) は市場オッズに既に織り込まれている可能性が高い。
   - 乗法的に係数を更にかけると過剰評価。
   - **市場ブレンディング** (`market_blend=BLEND_DEFAULT=0.78`) で市場暗黙率と混合し、楽観を機械的に打ち消す。
2. **1 着率を直接使う**: 累計 1 着率 % をベース。距離・コース・馬場適性で大きく変動するため、検索 MCP の補強根拠で補正する。
3. **連対率・3 連対率の流用**: 「a が 1 着の時 b が 2 着」を a 非依存に近似。実際には騎手相性 / 競走馬の連携で変動するが、本リポジトリでは line bonus を持たない (競輪と違い line 概念がない)。

### LightGBM softmax 温度スケーリング (Phase 21, holdout 291 races 由来)

`src/ev.py` の `_lgbm_predict` で `softmax(score / LGBM_TEMPERATURE)` を適用 (既定 `T = 0.4`)。holdout 291 races の log loss 最小化で T=0.4 がピーク (T=1 比 -0.089)、Plan H2 が 2 → 11 hits に大幅安定化 (ROI 125% → 132%)。

仮説: LightGBM lambdarank は ranking 学習なので絶対確率の sharpness は under-fit になり、softmax を sharpen することで P(win) の calibration が改善する。Plan H2 (確率上位 + P×O ≥ 1.0) は確率の絶対値が picks に効くため恩恵が大きい。Plan H1 (純粋に確率順位) は影響軽微で +EV を維持。

注意: T=0.4 は in-sample fit。N=291 で確度はやや限定。lgbm 再学習時は holdout の log loss を再計測して T を確認 (`python -m src.eval_holdout --temperature <T>` で sweep 可能)。

### bet-type-specific market_blend (Phase 19-23 の旅, 結論: 全 plan で β=0.78)

`src/eval_holdout.py` の real-odds 評価で β の最適値が bet type ごとに違うように見えたが、**段階的な robustness 検証で全て overfit と判明**した:

1. **Phase 19** Plan H1 → β=0 (in-sample ROI 109%)
   → **Phase 22**: 5-fold CV で β=0 不安定 (mean hold-out 64.7%)、revert
2. **Phase 21 follow-up** Plan H2 → β=0 (in-sample ROI 132%)
   → **Phase 22**: 同 CV で β=0 不安定 (mean hold-out 64.0%)、revert
3. **Phase 20** Plan G → β=1.0 (in-sample ROI 108%、CV では β=1.0 stable)
   → **Phase 23**: sliding-window で新規 LGBM 訓練、Window 4 (valid 1471-1634, n=149) で **Plan G hit 0/149** → combined 5/440 races, ROI ~71% < 77.5%、revert

production 設定 (Phase 23 後):

| 構成要素 | 値 | 根拠 |
|---|---|---|
| 全 Plan + 単勝/3 連単 EV table | **β=0.78** (`BLEND_DEFAULT`) | 単勝 ROI peak 95.9%。多 Plan で in-sample 「+EV」は overfit と判明したので保守的 default |
| LGBM softmax | **T=0.4** (`LGBM_TEMPERATURE`) | 5-fold CV で T std=0.02 と robust、out-of-sample 改善 -0.089 log loss (in-sample と一致)。**全 Phase 中で唯一 CV 通過した変更** |

`src/ev.py` には実験用に `BLEND_HIT_PURE = 0.0` と `BLEND_APTITUDE_GATE = 1.0` の定数を残置。`src/analyze.py` は `plan_rows_hit` / `plan_rows_apt` を計算して引数として伝搬するが production の Plan logic では未使用 (将来データ蓄積後の再 sweep 用)。

### Plan の実証的な階層 (N=291 + W4 N=149 合算)

| Plan | 累計 hit | 累計 ROI | コメント |
|---|---|---|---|
| A | 1+0 / 440 | ~10% | hit 極小、ノイズ支配 |
| B | 0+0 / 440 | 0% | 全 N で 0 hit、楽観バイアスの罠 |
| C | 2+0 / 440 | ~10% | 同 A |
| G | 5+0 / 440 | ~71% | W3 で +EV に見えたが W4 で 0/149 失敗、推定 -EV |
| H1 | 4+7 / 440 | 60-70%  | 単一 hit dependent、N=440 でも +EV 不確定 |
| H2 | 4+3 / 440 | 70-90% | やや好調だが N が小さく断言できず |

**結論: 現状の N では +EV を確証できる Plan は存在しない**。production の β=0.78 + T=0.4 は controlled 最少 phase で「市場+モデル」のブレンドのみ。レース 1000+ 蓄積後に再 sweep して確認する必要あり。

Plan A/B/C は検索 MCP の補強根拠で慎重にフィルタし、Plan G/H1/H2 は当て枠として小ロット試行することを推奨。

### 唯一 robust に確認された +EV 設定: 単勝 β=0.78

`scripts/sliding_window_eval.py` で 2 つの独立 validation window で単勝 ROI を β sweep した結果:

| Window | β peak | β=0.78 ROI | 市場 (β=1.0) ROI | Δ |
|---|---|---|---|---|
| W3 (train 0-1308, valid 1308-1634, n=291) | β=0.75-0.80 | 95.9% | 88.5% | **+7.4 pt** |
| W4 (train 0-1471, valid 1471-1634, n=149) | β=0.80 | 88.3% | 80.4% | **+7.9 pt** |

両 window で β=0.78-0.80 が peak で一致、市場に 7-8 pt 勝つ。これは **Plan-level の eval (N が極小) と違い 1 レース 1 ベットの単勝 ROI なので統計量が大きく**、N=149 でも 53 hits / 149 races の評価で意味ある差が出る。

→ **単勝賭けに限れば β=0.78 + T=metadata 由来 は実用的な +EV 戦略**。3 連単 / 馬連等は N が足りないので慎重に。

### race-class 別の signal 強度 (`scripts/race_class_diagnostic.py`)

production model + β=0.78 + T=0.45 で W3 valid (n=291) の **top-1 単勝**の hit/ROI を race 特性別に diagnostic した結果:

| filter | n | hit% | ROI |
|---|---|---|---|
| ALL | 291 | 44.7% | 92.9% |
| ダート | 126 | 56.3% | 97.8% |
| **Sprint ≤1300m** | 36 | **63.9%** | **115.3%** ★ |
| **confidence 0.25-0.35** | 101 | 44.6% | **105.7%** ★ |
| confidence ≥0.35 | 115 | 57.4% | 85.7% (favorite-heavy) |
| confidence 0.15-0.25 | 75 | 25.3% | 86.7% |

**confidence 0.25-0.35 band も sliding-window で再現せず:**
- W3 (n=101): 105.7% ROI (in-sample 発見)
- W4 (n=68, 新規モデル): **86.9% ROI** (再現しない)
- **combined (n=169): 98.1% ROI** — break-even 未満

→ Plan G β=1.0 と同じパターン: in-sample で +EV に見えた bin discovery が
sliding-window で破綻。N=291 程度の post-hoc bin 切り分けは overfit と判断。

**NAR ダート bias 注意**: validation set は時系列後半 = NAR ダート に偏る (芝 0 race)。"Sprint" finding は実質「NAR Sprint ダート」。JRA 芝 への transfer は別途検証必要。Sprint も sliding-window 同 race set なので bin の robustness 検証不能。

**結論**: bin selection を上から見つけて適用する戦略は本データでは効かない。**confidence band で race を選別するのではなく、常に β=0.78 で top-1 単勝を打ち続けるほうが robust**。レース蓄積後に再 sweep して band を再確認する。

### Plan B の経験的弱さ (holdout 観察)

n=291 races の real-odds 評価で **Plan B (最高 P×O 上位 3 点) は全 β で hit 0 / 0% ROI**。Plan A も β=0.4-0.80 で hit 0、β=0.85-0.90 で hit 1。これは N が小さくて結論できない (Plan B 全 picks = 873、期待 hit ≈ 3-10) が、傾向として:

- 「最高 P×O」フィルタは model_p > market_implied_p の **outsider triple** を集中的に選ぶ
- これらは確率モデルの楽観バイアスが最も出る領域 — model_p が真の hit rate より高めに出る
- 結果、Plan B の picks は「+EV に見える outsider」ばかりで現実には外れる
- 既存の `PXO_FLOOR=1.02` ではこの落とし穴を防ぎきれない可能性

対策候補 (未実装、レース蓄積後に再検討):
- Plan B / C に `--min-prob` のような確率下限を強制適用 (現状 CLI から渡せるが既定なし)
- 確率モデルの calibration (Platt scaling / isotonic regression) で楽観を均す
- Plan B の `PXO_FLOOR` だけ 1.02 → 1.10 など引き上げる

### 保守化の哲学

- **EV を膨らませる係数は控える**。1 倍に近づけるのが基本姿勢。
- **複数の正の補正を積み重ねない**。
- **YAML で確率を手動上書き** (`--probs data/probs/<race>.yaml`) する余地を残す。
- **検索 MCP の補強根拠** を最終フィルタにする。確率が楽観でも、補強根拠 0 件の目は Plan に乗せない。

## 分析フロー (必ずこの順)

### Step 1 — 出馬表の精査

- 馬番 / 枠 / 性齢 / 斤量 / 馬体重 / 騎手 / 厩舎 を確認。
- **取消・除外があれば全分析を破棄してやり直す**。

### Step 2 — 個別馬の 1/2/3 着率を精査

- 直近 5 走の着順データを必ず確認。これが分析の核心。
- 構造的ミスプライスの典型:
  - **3 着スペシャリスト** (3 着率突出): 過小評価され、人気外の 3 着オッズが残る。
  - **2 着スペシャリスト** (2 着率突出): 1-X-X / X-N-X の N に置いた目で +EV が出やすい。
  - **距離 / コース適性が抜群** の馬が人気薄で残ってる場合。

### Step 3 — 実オッズと突き合わせて P×O 計算

- **実オッズなしに推測で EV を出さない**。
- 順位 51 位以下に +EV が集中することが多い。**51–150 位**まで必ず確認。

### Step 4 — 市場バイアスを認識

中央競馬市場の頑強な構造的バイアス:

- **人気馬 (1 倍台 / 2 倍台) の 1 着過大評価** → 1-X-X が過熱、低オッズに集中。
- **3 着・2 着スペシャリストの軽視** → 下位順位に +EV が滞留。
- **騎手人気バイアス** (ルメール / 川田 / 武豊 等) → 騎手だけで人気になる馬は過大評価。

### Step 5 — 「広め」と「集中」を使い分け

- ユーザーが **「広め」** → 6–12 点 (Plan C 上限 12 点)。
- ユーザーが **「集中」** → 1–3 点 (Plan B)。
- デフォルト → **5 点バランス** (Plan A: 本線 2 / 中穴 2 / 大穴 1)。

### Step 5.5 — 受け入れ最大 EV / 最低当選率の指定があれば従う

CLI / Makefile の `--ev-max` / `--min-prob` を尊重。

## 出力フォーマット

各分析で必ず以下を出力:

1. **P×O ランキング** — 上位 +EV 候補を表で
2. **推奨セット 3 案** — Plan A (推奨) / Plan B (最高 EV) / Plan C (中庸・保険型)
3. **シナリオ別の的中目** — どの展開で何が当たるか
4. **重要判断ポイント** — オッズ変動時の判断基準

## 確率推定の典型値 (参考)

| 状況 | 1 着確率 |
| --- | --- |
| 単勝 1 倍台の超人気馬 | 40–55% |
| 単勝 2-3 倍台の人気馬 | 25–35% |
| 単勝 5-8 倍台 | 12–18% |
| 単勝 10-20 倍台の中穴 | 5–10% |
| 単勝 30 倍以上 | 1–4% |

## 禁則

- データ上 **着率 0%** の馬をその着順に置いた目は **全カット**。
- 朝のオッズで賭けない。**発走 5 分前のオッズが最も信頼**。
- 取消・除外があれば全分析を破棄してやり直す。
- 「想定平均オッズ × 想定的中率 = EV」のテンプレに乗らない。**必ず点数で割る**。
- 市場が効率的なレース (EV ≤ 1) を無理に打たない。

## 検索 MCP の運用ルール (的中率・回収率の底上げ)

このリポジトリの `claude -p` 評価セッションでは **Brave Search MCP** と **Tavily MCP** が利用可能。

### 検索すべき情報 (優先度順)

1. **馬の直近 5 走の着順詳細・距離適性・コース実績** (netkeiba の累計 rate だけでは波形が見えない)
2. **騎手の当該コース成績 / 主戦騎手 vs 乗り替わり**
3. **当日の馬場状態** (高速 / 重 / 渋り) と当該馬の馬場適性
4. **厩舎調整 / パドック気配 / 馬体重変化の所感**
5. **取消・除外・体調不安の有無** (絡む目を全カットする根拠)
6. **過去対戦履歴** (重賞では特に重要)

### 検索すべきでないこと

- netkeiba から取得済みの基本データ (馬名・騎手・斤量・馬体重・オッズ・人気)
- 競馬の基本ルール・配当計算式
- 1 か月以上前の汎用情報

### 検索クエリのテンプレ

```
"<馬名>" 直近 5 走
"<馬名>" <距離>m <芝 OR ダート>
"<騎手名>" <競馬場名> 成績
<競馬場名> 馬場状態 <YYYYMMDD>
"<馬名>" 取消 OR 除外 OR 体調
```

### 検索予算

- 1 レースあたり **最大 6 クエリ** (Brave + Tavily 合計)
- 検索の優先対象は **P×O ≥ 2.0 の上位 8 候補**にのみ

### 検索結果に基づく加点・減点ルール

| 検索で見つかった根拠 | アクション |
| --- | --- |
| 距離 / コース / 馬場適性が良い | **+補強根拠 1** |
| 直近 5 走で 2-3 着率突出 | **+補強根拠 1** |
| 騎手が当該コース得意 | **+補強根拠 1** |
| 馬体重大幅減 (-10kg 超) / 大幅増 (+10kg 超) | **−補強根拠 1** |
| 取消 / 除外 / 体調不安 | **絡む目を全カット** |
| 検索しても確証なし | 「補強根拠なし」として Plan 入り保留 |

### Plan 入りの最終ルール

- **コア** (補強 3 件以上) → 必ず Plan A/B に含める、点数厚め
- **採用** (補強 2 件以上) → Plan A 候補
- **保留** (補強 1 件のみ) → Plan C のみ
- **却下** (補強 0 件) → Plan から外す
- **絶対却下** (取消 / 致命的マイナス) → 全廃棄

## このリポジトリの使い方

```bash
# 初回セットアップ
make setup    # venv + Playwright + Chromium

# 分析 (URL から)
python -m src.analyze 'https://race.netkeiba.com/race/shutuba.html?race_id=202605210601'

# 確率を YAML で渡す場合
python -m src.analyze <url> --probs data/probs/<race_id>.yaml

# 発走前 5 分まで待機して refresh
python -m src.analyze <url> --refresh

# キャリブレーション
make record RACE=20260521-521-1 ORDER=5,2,7 PAYOUT=25400
make calibrate

# 学習データ蓄積後の holdout 評価 (β の妥当性を再確認)
make holdout                                # 全 β sweep + 3 連単 PL eval
python scripts/fetch_trifecta_odds_holdout.py  # validation 291 races の trifecta odds を scrape
                                             # → これで `make holdout` が Plan A/B/C/H1 real-odds ROI も出す

# FastAPI バックエンド + Next.js フロント
make api      # uvicorn :9788  (keirin ev-api 8787 と完全にずらす。「788」は keiba シグネチャ)
make web      # next dev :3788 (keirin web 3000 と被らないように)
```
