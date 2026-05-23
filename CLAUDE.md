# keiba-ev — 中央競馬 EV 分析プロジェクト

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

1. **適性指数 (`src/aptitude.py`)**: 各馬の 8 因子 (能力 / 距離適性 / 末脚 / 馬場 / 状態 / 騎手 / ペース fit / 重賞実績) を 0-100 でレース内正規化。総合は重み付け平均。
2. **確率モデル (`src/ev.py:estimate_probs`)**: Layer 1 特徴量 + 市場ブレンド + Discounted Harville で win/place2/place3 確率を出す。Plackett-Luce 連鎖で 3 連単・3 連複・馬連・ワイド・馬単・単勝・複勝 すべての確率を導出。
3. **複数 bet type の EV**: 単勝 / 複勝 / 馬連 / ワイド / 馬単 / 3 連複 / 3 連単 を同じ確率モデルで EV table 化。控除率の低い bet type (単複 20%、馬連 22.5%) は +EV が残りやすい。
4. **Plan G (適性ゲート → EV 足切り)**: 適性総合 top N 頭 (デフォルト 6) の集合内で生成される買い目のみ → P×O ≥ 1.02 で足切り。EV-first の Plan A/B/C と並列で提案される、競馬独自の「適性で選んで EV で確認」戦略。
5. **検索 MCP 補強**: LLM (`claude -p`) が適性指数 + Plan G を受け取って、検索で根拠を検証 / 補強根拠数で再ランク。

snapshot に保存される主要フィールド:
- `horse_aptitude`: 各馬の指数 + 内訳 (total 降順)
- `aptitude_top_horses`: Plan G の集合
- `plan_a_keys` / `plan_b_keys` / `plan_c_keys` / `plan_g_keys` / `plan_h1_keys` / `plan_h2_keys` / `plan_f_keys` (3 連単)
- `bet_tables`: 単勝 / 複勝 / 馬連 / ワイド / 馬単 / 3 連複 の EV top 30
- `bet_tables_g`: 各 bet type の Plan G picks

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
