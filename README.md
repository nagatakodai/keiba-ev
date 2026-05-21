# keiba-ev

中央競馬 (JRA) の 3 連単について **EV (期待値) > 1** の買い目を netkeiba から抽出するローカル分析ツール。

`ev` / `ev-api` (競輪向け) を keiba 用に移植したもの。**ローカル完結** (Vercel / GCS / Cloud Run には依存しない)。

- 分析方針・EV 計算式・市場バイアスの扱いは `CLAUDE.md`
- データソースは [netkeiba.com](https://race.netkeiba.com/) (出馬表・3 連単オッズ・結果)

## 構成

```
keiba-ev/
├── src/                # Python バックエンド (CLI + ライブラリ)
│   ├── analyze.py      # メインエントリ (race_id URL → P×O ランキング + Plan A/B/C)
│   ├── parse.py        # netkeiba HTML → RaceData
│   ├── scrape.py       # Playwright で HTML 取得
│   ├── ev.py           # 確率推定 + Plackett-Luce 連鎖
│   ├── models.py       # Horse / Race / Probabilities / EvRow
│   ├── llm.py          # claude CLI spawn + framework プロンプト
│   ├── auto_watch.py   # 当日の開催を polling して発走前に自動解析
│   ├── fetch_result.py # 結果ページから着順・払戻を自動取得
│   ├── record.py       # 手動で結果記録
│   └── calibrate.py    # tier 別 / Plan 別 ROI 集計
├── api/                # FastAPI バックエンド (ローカル開発用)
│   ├── main.py
│   ├── runner.py
│   ├── store.py
│   └── _watch_loop.py
├── web/                # Next.js フロントエンド
│   ├── app/
│   ├── components/
│   └── lib/
├── Makefile
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

### MCP サーバ (任意 / Claude 評価で使う Brave Search + Tavily)

```bash
npm install       # ./node_modules/.bin/{mcp-server-brave-search,tavily-mcp} を取得
cp .env.example .env
# .env を編集して BRAVE_API_KEY / TAVILY_API_KEY / ANTHROPIC_API_KEY を記入
```

`make run` 時に `.env` が `python-dotenv` で読まれ、spawn する `claude` CLI に継承されます。

### フロントエンド

```bash
make web-install  # pnpm install (or npm install) を web/ で実行
make web          # next dev (デフォルト localhost:3000)
```

## 使い方

### URL から解析

netkeiba の出馬表 / オッズ URL (`race_id` を含むもの) を渡す:

```bash
. .venv/bin/activate
python -m src.analyze 'https://race.netkeiba.com/race/shutuba.html?race_id=202605210601'
# or make run URL='...'
```

`race_id` は `YYYYMMDDPP00RR` 形式 (例 `202605210601` = 2026/05/21 阪神 (06) 1R)。

実行内容:

1. 出馬表 HTML を取得・パース (馬番 / 馬名 / 騎手 / 馬体重 / 過去戦績)
2. 3 連単オッズ HTML を取得・パース
3. 確率モデル (1着率 × レーティング + 市場ブレンド) で P を推定
4. P×O ランキング・Plan A/B/C/H1/H2/F を出力
5. (任意) `claude` CLI を spawn し、framework + 検索 MCP で各馬を評価

### 確率を YAML で上書き

`data/probs/<race_id>.yaml`:

```yaml
win_prob:
  1: 0.18
  4: 0.30
  7: 0.28
place2_prob:
  1: 0.22
place3_prob:
  1: 0.20
```

```bash
python -m src.analyze <url> --probs data/probs/<race_id>.yaml
```

### HTML 貼付モード (Playwright が動かない時)

```bash
python -m src.analyze --html shutuba.html --odds-html odds.html
```

### Plan キャップ

```bash
make run URL='...' EV_MAX=3 MIN_PROB=2.0
#   EV_MAX   : Plan に組む最大 P×O (大穴除外)
#   MIN_PROB : Plan に組む最低当選率 % (低当選率除外)
```

### 発走前 Refresh

「初回分析 → 発走 5 分前まで待機 → 再取得 → 差分表示 → 再評価」を 1 コマンド:

```bash
python -m src.analyze <url> --refresh
# デフォ 5 分前。--refresh-min 3 で 3 分前に変更
```

### watch モード (URL を貼り続ける)

```bash
make watch EV_MAX=3 MIN_PROB=2.0
```

### watch-auto (発走前に自動発火)

```bash
make watch-auto WINDOW=5 TOLERANCE=4 INTERVAL_SEC=60
# 中央競馬は土日中心 9:30-17:30 で動かす
```

### キャリブレーション

```bash
# 結果記録 (auto fetch が拾えなかった時の手動入力)
make record RACE=20260521-521-1 ORDER=5,2,7 PAYOUT=25400

# 集計
make calibrate
make calibrate PER_RACE=1
```

`data/results/` は git 追跡対象でマシン間共有可。

### FastAPI バックエンド + フロント (UI)

```bash
make api          # uvicorn --reload :8788  (keirin ev-api と被らないように 8788)
# 別ターミナルで
make web          # next dev :3000
```

ブラウザで http://localhost:3000 を開く。

## netkeiba 構造の前提

netkeiba は HTML が時期で変わりやすい。`src/parse.py` は best-effort パーサで、以下を仮定:

- 出馬表: `.Shutuba_Table` 配下に `tr.HorseList`
- 3 連単オッズ: `<script>` 中の `"1-2-3":"12.3"` 形式 (なければ DOM フォールバック)
- 結果: `table.ResultTableWrap` の着順と `table.Payout_Detail_Table` の三連単

崩れたら `src/parse.py` の selector / regex を更新する。

## EV / 確率モデルの詳細

`CLAUDE.md` を参照。要点:

- 中央競馬の 3 連単控除率 ≒ 22.5%。`P × O = 1.0` が +EV ライン。
- 楽観バイアス回避のため Plan 入りフロアは `P × O ≥ 1.02`。
- 確率モデルは **市場ブレンディング** (`market_blend=0.4`) で市場暗黙率と混合。
- 各順位 (1/2/3) に固有 strength を持つ **Plackett-Luce 連鎖**。
- 競輪と違い line 概念がないので pair_factor / line_bonus は無し。

## 既知の制約

- netkeiba 結果ページのパーサは降着・取消などの非正規ケースは未対応
- 3 連単オッズの全件取得は netkeiba の JS 構造に依存。Playwright が遅い時はオッズが揃わない可能性あり
- 自動キャリブレーションは未実装 (手動判断の参考データ)
