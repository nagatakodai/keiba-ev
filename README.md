# keiba-ev

中央 (JRA) + 地方 (NAR) 競馬の全 7 券種について **EV (期待値) > 1** の買い目を抽出し、Claude が選定した「総合オススメ束」を提示するローカル分析ツール。オッズパーク経由で **自動投票 (実弾) まで可能**。

`ev` / `ev-api` (競輪向け) を keiba 用に移植したもの。**ローカル完結** (Vercel / GCS / Cloud Run には依存しない)。

- 詳細な分析方針・確率モデル・市場バイアスの扱い・運用フローは `CLAUDE.md`
- データソース:
  - **live (発走前)**: NAR は [keiba.go.jp 公式](https://www.keiba.go.jp/) (全6券種)、JRA は [JRA 公式](https://www.jra.go.jp/) (全7券種)。発走時刻は [競馬ブック](https://p.keibabook.co.jp/cyuou/top) / [オッズパーク](https://www.oddspark.com/) から取得
  - **past (過去レース)**: [netkeiba.com](https://race.netkeiba.com/) (出馬表・馬柱・結果)
  - 投票実行: [オッズパーク](https://www.oddspark.com/) (Playwright で半自動 or 全自動)

## 構成

```
keiba-ev/
├── src/                # Python バックエンド (CLI + ライブラリ)
│   ├── analyze.py            # メインエントリ (URL → 確率推定 + bet table + 2 bundle)
│   ├── aptitude.py           # 各馬の 9 因子適性指数
│   ├── parse.py              # netkeiba HTML → RaceData
│   ├── scrape.py             # netkeiba Playwright fetch (過去レース取得用)
│   ├── scrape_keibago.py     # NAR 公式 (keiba.go.jp) 全6券種 (live odds 用)
│   ├── scrape_jra.py         # JRA 公式 (accessO.html token walk) 全7券種 (live odds 用)
│   ├── scrape_oddspark.py    # オッズパーク (NAR 二次 fallback + race list discovery)
│   ├── scrape_alt.py         # 競馬ブック (JRA 発走時刻 / live discovery)
│   ├── ev.py                 # 確率推定 + Plackett-Luce 連鎖 + 全 bet type EV table
│   ├── portfolio.py          # joint Kelly 最適まとめ買い束 (回収優先 + 的中優先)
│   ├── models.py             # Horse / Race / Probabilities / EvRow
│   ├── llm.py                # claude CLI spawn + framework プロンプト
│   ├── auto_watch.py         # 当日開催を polling して発走前に自動解析
│   ├── oddspark_bet.py       # オッズパーク投票 (半自動 / 全自動 / 常駐 daemon)
│   ├── fetch_result.py       # 結果ページから着順・払戻を自動取得
│   ├── record.py             # 手動で結果記録
│   ├── bulk_fetch.py         # 過去レース一括取得 (学習データ用)
│   └── calibrate.py          # tier 別 / bundle 別 hit / ROI 集計
├── api/                # FastAPI バックエンド (ローカル開発用)
│   ├── main.py
│   ├── runner.py
│   ├── store.py
│   └── _watch_loop.py
├── web/                # Next.js フロントエンド
│   ├── app/                  # dashboard / predictions / calibrate / watch-auto / analyze
│   ├── components/
│   └── lib/
├── Makefile
├── CLAUDE.md           # 運用方針 / 確率モデル / 検索 MCP ルール / 開発フロー
├── requirements.txt    # Python 依存
└── package.json        # MCP サーバ (Brave Search / Tavily) 用
```

## セットアップ

### Python (バックエンド)

```bash
make setup        # python3.13 + venv + pip install + Playwright Chromium
# python3.13 がない環境 (WSL Ubuntu 等) は:
make setup-uv     # uv 経由で 3.13 を入れる
```

### Claude CLI (LLM 評価 / web 検索補強)

LLM 評価は Anthropic API を直叩きせず、ローカルの `claude` CLI を `claude -p` で subprocess spawn します (`src/llm.py`)。

```bash
# Claude Pro / Max サブスクリプションでログイン (推奨)
claude login
```

サブスクリプションでログイン済みなら `ANTHROPIC_API_KEY` は不要です。

### MCP サーバ (任意 / Claude 評価で使う Brave Search + Tavily)

```bash
npm install       # ./node_modules/.bin/{mcp-server-brave-search,tavily-mcp} を取得
cp .env.example .env
# .env を編集して BRAVE_API_KEY / TAVILY_API_KEY を記入
```

`make run` 時に `.env` が `python-dotenv` で読まれ、spawn する `claude` CLI に継承されます。

### オッズパーク認証 (任意 / 自動投票で使う)

実弾自動購入 (`make bet`) / Web UI の自動投票 (自動ログイン) 用。`.env` に追記:

```
ODDSPARK_ID=...
ODDSPARK_PASSWORD=...
ODDSPARK_PIN=...
```

PIN は普段使ってない端末でログイン時に oddspark が追加認証として要求するため。

`make run` / `make bet` / `make api` はいずれも起動時に `.env` を `python-dotenv` で読み込むので、ここに書けば
Web UI (watch-auto ページ) の **自動ログイントグル** でも認証情報が daemon に継承される (env が空だと daemon は
手動ログインにフォールバックしてブラウザを開いたまま待つ)。認証情報はコード / ログ / コミットに残さない。

### フロントエンド

```bash
make web-install  # pnpm install (or npm install) を web/ で実行
make web          # next dev :3788 (keirin web 3000 と被らないように)
```

## 使い方

### URL から解析 (手動 / 単発)

```bash
. .venv/bin/activate
python -m src.analyze 'https://race.netkeiba.com/race/shutuba.html?race_id=202605210601'
# or make run URL='...'
```

`race_id` は `YYYYVVKKDDRR` 形式 (例 `202605210601` = 2026/05/21 阪神 (06) 1R)。

実行内容:

1. 出馬表 + 馬柱 + 全 7 券種オッズを取得・パース
2. 9 因子適性指数 + 確率モデル (市場ブレンド + Plackett-Luce) で各 outcome の P を推定
3. 各 bet type の EV table (回収優先・P×O 降順) + 的中優先 table (prob 降順 + px_o≥1.0 floor)
4. **joint Kelly 最適まとめ買い束** を 2 つ生成:
   - `recommended_bundle` (回収優先) ← 実弾で買う対象
   - `recommended_bundle_hit` (的中優先) ← おまけ計測のみ
5. (任意) `claude -p` を spawn し、Brave / Tavily で per-leg 補強根拠を集めさせて bundle を選定 / 検証

### Plan キャップ / 適性ゲート / 多 bet type フラグ

```bash
make run URL='...' MARKET_BLEND=0.8 APTITUDE_TOP=6
#   MARKET_BLEND  : 市場暗黙率とモデルの混合比 β (default 0.78)
#   APTITUDE_TOP  : 適性 top N 頭 (default 6)
#   EV_MAX        : EV table の最大 P×O (大穴除外)
#   MIN_PROB      : 最低当選率 % (低当選率除外)
```

`make bet` で自動投票する場合のデフォルトは `WINDOW=1 MARKET_BLEND=0.8 MIN_PROB=0.5` 等 (下記)。

### Bundle (旧 Plan A/B/C/F/G/H1/H2 は廃止)

2026-05-29 の restructure で集計対象を **2 bundle のみ** に集約。

- **回収優先 (`recommended_bundle`)**: joint Kelly で E[log W] を最大化する EV 最適束。トリガミ防止フィルタ (margin=1.10) 付き。**実弾で買う対象**。
- **的中優先 (`recommended_bundle_hit`)**: prob 降順で pool を絞ったうえで Kelly 配分。確率高い目を抑える戦略。**おまけ計測のみ** (買わない、ダッシュボードで的中率 / ROI 集計だけする)。

bet type は 単勝 / 複勝 / 馬連 / ワイド / 馬単 / 3連複 / 3連単 を全て同じ確率モデルで EV 計算。3連単 も他券種と並ぶ `bet_tables` の一員。Claude が両 bundle を選定する。

### 発走前 Refresh

「初回分析 → 締切 N 分前まで待機 → 再取得 → 差分表示 → 再評価」を 1 コマンド:

```bash
python -m src.analyze <url> --refresh
```

### watch-auto (発走前に自動発火)

```bash
make watch-auto WINDOW=5 TOLERANCE=4 INTERVAL_SEC=60
```

`WINDOW` は **締切までのリード時間 (分)**。締切=発走 2 分前固定なので、`WINDOW=5` は発走 7 分前に dispatch。

discovery は **公式ソース** から (netkeiba live は使わない):
- NAR: oddspark の当日 race list (発走時刻つき)
- JRA: 競馬ブック (発走時刻) × JRA 公式 `discover_jra_races` (netkeiba_rid) を場名+R で join

analyze は NAR=keibago / JRA=JRA 公式。netkeiba は data/raw/ の **過去レースキャッシュと学習データ用途のみ**。

### `make bet` — watch-auto + 自動投票 (実弾 / 半自動)

```bash
make bet
# ↑ 推奨デフォルト: WINDOW=1, MARKET_BLEND=0.8, MIN_PROB=0.5, APTITUDE_TOP=6,
#   SESSION_ARGS=--auto-purchase --auto-login --clear --payment=buylimit
#                --stake-multiplier=2 --daily-cap=50000 --poll=5
```

挙動:
- 投票ブラウザ daemon (`oddspark_bet --session`) を headful で起動 → env で自動ログイン (or 手動ログイン)
- watch-auto ループが裏で回り、締切 1〜5 分前のレースを解析
- 解析後 `recommended_bundle` (回収優先) が空でなければ daemon の queue (`data/cache/oddspark_bet_queue/`) に投入
- daemon がカート投入 + `--auto-purchase` で `#gotobuy → 確認 → #buy` まで自動確定 (実弾)
- 安全四段: ① `AUTO_PURCHASE_VERIFIED=True` フラグ ② per-race ¥10,000 上限 ③ `--daily-cap=50000` 日次上限 ④ success marker 検出後にのみ daily_stake 加算

部分上書き:

```bash
make bet WINDOW=3                              # 発走 5 分前 dispatch
make bet MARKET_BLEND=0.9                       # 市場寄せを強める
make bet SESSION_ARGS="--auto-purchase --clear --payment=opcoin --stake-multiplier=1"
```

### 浦和スキップ等の場別フィルタ

`src/auto_watch.py:BET_SKIP_VENUES` に場名を追加すると、analyze / snapshot は通常通り走るが enqueue (= 自動投票) だけ skip:

```python
BET_SKIP_VENUES: set[str] = {"浦和", "船橋"}   # 投票対象から外す
```

### キャリブレーション

```bash
# 結果記録 (auto fetch が拾えなかった時の手動入力)
make record RACE=20260521-521-1 ORDER=5,2,7 PAYOUT=25400

# 集計
make calibrate
```

dashboard (`/`) / 確率較正 (`/calibrate`) に **回収優先 / 的中優先 AI** の hit_rate + ROI (見送り除外) が出る。

### FastAPI バックエンド + フロント (UI)

```bash
make api          # uvicorn --reload :9788 (keirin ev-api 8787 と完全にずらす。「788」は keiba シグネチャ)
# 別ターミナルで
make web          # next dev :3788 (keirin web 3000 と被らないように)
```

ブラウザで http://localhost:3788

- **ダッシュボード (`/`)**: watch-auto 稼働状況 + 集計レース数 + **回収優先AI** (実弾で買う・的中率/回収率) セクションと **回収優先のみのチャート** (累積収支 / 回収率推移 / 結果分布 / bet 種別)。その下に **的中優先AI** (おまけ計測・買わない) セクションと専用チャート (緑系・bet 種別含む)
- **確率較正 (`/calibrate`)**: tier ratio (実hit/予測P) + 回収優先 / 的中優先 bundle の実績集計 + race 毎の 2 列 grid (見送りはグレー bg、回収優先=青バッジ / 的中優先=緑バッジ)。※旧 Plan A/B/C 別テーブルは廃止
- **予測詳細 (`/predictions/<race_id>`)**: 回収優先まとめ買い (Claude 選定 / full Kelly + ½ Kelly 併記) + **的中優先まとめ買い** (おまけ計測・緑系) + 全 bet type EV table + 適性 / 馬体 / 馬場
- **watch-auto (`/watch-auto`)**: 開始 / 停止 + 直近履歴 (回収優先 / 的中優先 picks) + **オッズパーク自動投票トグル** (カート投入) と **自動ログイントグル** (env 認証で daemon が自動ログイン / OFF は手動)

### 過去レース一括取得 (学習データ蓄積)

```bash
python -m src.bulk_fetch --since 20260101 --until 20260531 --workers 2 --polite-ms 2000
# or
make bulk-fetch SINCE=20260101 UNTIL=20260531 WORKERS=2

# 既存 rids list を再利用してレジューム
python -m src.bulk_fetch --rids-file=data/cache/rids_year_2026.txt \
  --since 20260101 --until 20260531 --workers 2 --polite-ms 2000
```

netkeiba の出馬表 + 馬柱 + 結果を `data/raw/*.html.gz` に gzip 保存。

## データソースの使い分け

| 用途 | ソース | 備考 |
|---|---|---|
| **live odds (発走前)** NAR | keiba.go.jp 公式 (`scrape_keibago.py`) | 全6券種、組合せ明示、誤オッズ無し |
| **live odds (発走前)** JRA | JRA 公式 (`scrape_jra.py`) | 全7券種、accessO.html token walk |
| **live race discovery** NAR | オッズパーク (`scrape_oddspark.fetch_race_list_oddspark`) | netkeiba_rid + 発走時刻 |
| **live race discovery** JRA | 競馬ブック (`scrape_alt.fetch_race_list_keibabook`) × JRA 公式 `discover_jra_races` | 場名+R で join して netkeiba_rid + 発走時刻 |
| **live odds fallback** NAR | オッズパーク (単複/3連単のみ) | keiba.go.jp が解決できない場合 |
| **過去レース (学習データ)** | netkeiba (`scrape.py`, `bulk_fetch.py`) | 出馬表 / 馬柱 / 結果 |
| **結果 fallback** NAR | keiba.go.jp `fetch_keibago_result` | netkeiba block 中も着順 + 払戻取得 |
| **結果 fallback** JRA | JRA 公式 `fetch_jra_result` | 同上 |
| **投票実行** | オッズパーク (`oddspark_bet.py`) | Playwright で半自動 / 全自動 |

netkeiba は **live odds / live discovery では一切使わない** (IP 規制対策)。過去レースの解析や学習用 cache のみ。

## EV / 確率モデルの詳細

`CLAUDE.md` を参照。要点:

- 中央競馬の 3 連単控除率 ≒ 22.5%。市場効率では `P × O ≒ 0.775`。
- `P × O > 1.0` で理論上 +EV だが、確率モデルの楽観バイアスを考慮して **Plan 入りフロアは P × O ≥ 1.02**。
- 確率モデルは **市場ブレンディング** (`market_blend=0.78`) で市場暗黙率と混合 (Phase 22-23 で全 plan で β=0.78 が CV 通過した唯一の robust 設定)。
- 各順位 (1/2/3) に固有 strength を持つ **Plackett-Luce 連鎖**。
- LightGBM softmax 温度 `T=0.4` で sharpen (holdout 291 races で唯一 CV 通過した out-of-sample 改善)。

## 開発フロー

CLAUDE.md にも記載:

- **commit + main push は確認なしで OK** (2026-05-29 ユーザ許可)。論理的なまとまりで commit して main へ直 push。
- ただし依然として要確認: 破壊的操作 (reset --hard / force push / branch -D)、`.env` 等機密のコミット可能性、hooks の skip。
- commit message は日本語 conventional 風 (`feat:` / `fix:` / `refactor:`)。Co-Authored-By 付け。

## 既知の制約

- netkeiba 結果ページのパーサは降着・取消などの非正規ケースは未対応
- JRA 公式 (`accessO.html`) は **開催日 (土日)** のみ live、平日は accessO に race token 不在 → JRA 平日は手動分析のみ
- オッズパーク利用規約による自動化制限のリスクは使用者が負う
- 自動キャリブレーションは未実装 (calibrate は集計のみ、係数の自動更新は人判断)
