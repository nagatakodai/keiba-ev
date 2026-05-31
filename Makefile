.PHONY: setup setup-uv install browsers clean run run-haiku run-sonnet run-no-llm refresh verify watch watch-auto record fetch-result fetch-result-list fetch-result-process calibrate backtest bulk-fetch bulk-enum dataset train holdout api web web-install test watch-auto-bet watch-auto-ipat-bet bet

PY := .venv/bin/python
PIP := .venv/bin/pip

# Mac / Linux で python3.13 が apt / brew から入っている環境
setup: .venv/bin/python install browsers

.venv/bin/python:
	python3.13 -m venv .venv
	$(PIP) install --upgrade pip

# uv 経由 (WSL Ubuntu 等、python3.13 が無い環境)
# 事前に: curl -LsSf https://astral.sh/uv/install.sh | sh
setup-uv:
	uv venv --python 3.13 --clear .venv
	uv pip install -r requirements.txt --python $(PY)
	$(PY) -m playwright install chromium

install:
	$(PIP) install -r requirements.txt

browsers:
	$(PY) -m playwright install chromium

clean:
	rm -rf .venv data/raw/* data/cache/*

# --- 日常運用ショートカット ---
# 使い方:
#   make run URL='https://race.netkeiba.com/race/shutuba.html?race_id=...' \
#            [EV_MAX=3] [MIN_PROB=2.0] [MARKET_BLEND=0.6] \
#            [APTITUDE_TOP=6] [WITH_EXACTA=1] [WITH_TRIO=1]
#
# - APTITUDE_TOP: Plan G の適性 top N 頭 (default 6)
# - WITH_EXACTA / WITH_TRIO: 馬単・3 連複も fetch (jiku iteration で重い、+40s ずつ)
EV_MAX ?=
MIN_PROB ?=
MARKET_BLEND ?=
APTITUDE_TOP ?=
WITH_EXACTA ?=
WITH_TRIO ?=
CAP_ARGS := $(if $(EV_MAX),--ev-max $(EV_MAX),) \
            $(if $(MIN_PROB),--min-prob $(MIN_PROB),) \
            $(if $(MARKET_BLEND),--market-blend $(MARKET_BLEND),) \
            $(if $(APTITUDE_TOP),--aptitude-top $(APTITUDE_TOP),) \
            $(if $(WITH_EXACTA),--with-exacta,) \
            $(if $(WITH_TRIO),--with-trio,)

run:
	$(PY) -m src.analyze '$(URL)' --llm-model opus $(CAP_ARGS)

run-haiku:
	$(PY) -m src.analyze '$(URL)' --llm-model haiku $(CAP_ARGS)

run-sonnet:
	$(PY) -m src.analyze '$(URL)' --llm-model sonnet $(CAP_ARGS)

run-no-llm:
	$(PY) -m src.analyze '$(URL)' --no-llm $(CAP_ARGS)

refresh:
	$(PY) -m src.analyze '$(URL)' --refresh --llm-model opus $(CAP_ARGS)

verify:
	$(PY) -m src.analyze --help

# --- テスト ---
# uv pip install pytest --python $(PY) で pytest が入っている前提
test:
	$(PY) -m pytest tests/ -v

# --- キャリブレーション ---
#   make record RACE=2026052102-3-2 ORDER=5,2,7 [PAYOUT=25400] [NOTE='大外不利']
RACE ?=
ORDER ?=
PAYOUT ?=
NOTE ?=
record:
	$(PY) -m src.record '$(RACE)' '$(ORDER)' \
		$(if $(PAYOUT),--payout $(PAYOUT),) \
		$(if $(NOTE),--note '$(NOTE)',)

# --- レース結果の自動取得 ---
URL ?=
fetch-result:
	$(PY) -m src.fetch_result fetch '$(RACE)' '$(URL)'

fetch-result-list:
	$(PY) -m src.fetch_result list

fetch-result-process:
	$(PY) -m src.fetch_result process

PER_RACE ?=
POINT_COST ?= 100
calibrate:
	$(PY) -m src.calibrate \
		$(if $(PER_RACE),--per-race,) \
		--point-cost $(POINT_COST)

# --- バックテスト harness (log loss / Brier / ECE / top-K / market baseline) ---
# 使い方: make backtest [SINCE=2026440521] [UNTIL=20264406] [RELIABILITY=1]
SINCE ?=
UNTIL ?=
RELIABILITY ?=
backtest:
	$(PY) -m src.backtest \
		$(if $(SINCE),--since $(SINCE),) \
		$(if $(UNTIL),--until $(UNTIL),) \
		$(if $(RELIABILITY),--reliability,)

# --- 過去レース大量取得 (2026/01-今日の JRA+NAR 全レース shutuba/past/result) ---
# 使い方:
#   make bulk-enum SINCE=20260101 UNTIL=20260521          # race_id 列挙のみ
#   make bulk-fetch SINCE=20260101 UNTIL=20260521 WORKERS=5  # 本実行
BULK_SINCE ?= 20260101
BULK_UNTIL ?= 20260521
# scrape 並列数。過去 5 workers + polite=500ms で netkeiba block を誘発したので
# default は 3 workers + polite 1000ms に下げて再発防止。必要なら override OK。
WORKERS ?= 3
POLITE_MS ?= 1000
RIDS_FILE ?=
bulk-enum:
	$(PY) -m src.bulk_fetch --since $(BULK_SINCE) --until $(BULK_UNTIL) --enum-only

bulk-fetch:
	$(PY) -m src.bulk_fetch --since $(BULK_SINCE) --until $(BULK_UNTIL) \
		--workers $(WORKERS) --polite-ms $(POLITE_MS) \
		$(if $(RIDS_FILE),--rids-file $(RIDS_FILE),)

# --- 学習データセット構築 (data/datasets/all.parquet) ---
LIMIT ?=
dataset:
	$(PY) -m src.dataset build $(if $(LIMIT),--limit $(LIMIT),)

# --- LightGBM lambdarank 学習 (data/models/) ---
# tuned defaults (Phase 17): lr=0.03 / rounds=800 / leaves=24 / early_stop=100
# valid ndcg@5 = 0.572 (28 features, 1,634 races, best_iter 102)
LR ?= 0.03
ROUNDS ?= 800
LEAVES ?= 24
EARLY_STOP ?= 100
train:
	$(PY) -m src.train --lr $(LR) --rounds $(ROUNDS) --leaves $(LEAVES) --early-stop $(EARLY_STOP)

# --- holdout 評価 (chronological split で 単勝 ROI / top-K / market 比較) ---
# train.py と同じ last-20% を valid に固定。複数 β で loglinear blend ROI を出すので
# `estimate_probs(market_blend=β)` の正味効果が分かる。
HOLDOUT_VALID_FRAC ?= 0.2
holdout:
	$(PY) -m src.eval_holdout --valid-frac $(HOLDOUT_VALID_FRAC)

# --- 大量パイプライン: 列挙 → fetch → dataset → train → backtest 一気通貫 ---
# 使い方: make bulk-pipeline BULK_SINCE=20260101 BULK_UNTIL=20260521
bulk-pipeline:
	$(PY) -m src.bulk_fetch --since $(BULK_SINCE) --until $(BULK_UNTIL) --enum-only
	$(PY) -m src.bulk_fetch --since $(BULK_SINCE) --until $(BULK_UNTIL) --workers $(WORKERS) \
		--rids-file data/cache/rids_$(BULK_SINCE)_$(BULK_UNTIL).txt
	$(PY) -m src.dataset
	$(PY) -m src.train --lr $(LR) --rounds $(ROUNDS) --leaves $(LEAVES)
	$(PY) -m src.backtest --rerun

watch:
	@echo "watch mode: EV_MAX=$(EV_MAX) MIN_PROB=$(MIN_PROB) MARKET_BLEND=$(MARKET_BLEND) / Ctrl+D で終了"
	@while true; do \
		printf '\n\033[1;36mURL> \033[0m'; \
		if ! IFS= read -r url; then echo; break; fi; \
		[ -z "$$url" ] && continue; \
		$(PY) -m src.analyze "$$url" --refresh --llm-model opus $(CAP_ARGS) || true; \
	done

# 2段パイプライン: SCORE 帯 (締切 SCORE_WINDOW〜+SCORE_TOLERANCE 分前) で Claude 考察→各馬指数を
# キャッシュ → BET 帯 (締切 WINDOW〜+TOLERANCE 分前) で最新オッズ+指数→束→投票。BET 帯は締切
# 直前 (既定 1〜2.5 分前)、SCORE 帯はその手前 (既定 5〜7 分前) で重ならないように分離する。
WINDOW ?= 1
TOLERANCE ?= 1.5
SCORE_WINDOW ?= 5
SCORE_TOLERANCE ?= 2
LLM_BLEND ?=
BET_LEAD_SEC ?= 60
INTERVAL_SEC ?= 60
ACTIVE_HOURS ?= 09:00-23:45
BET_ODDSPARK ?=
BET_IPAT ?=
BET_ARGS := $(if $(BET_ODDSPARK),--bet-oddspark,) $(if $(BET_IPAT),--bet-ipat,)
BAND_ARGS := --score-window $(SCORE_WINDOW) --score-tolerance $(SCORE_TOLERANCE) --bet-lead-sec $(BET_LEAD_SEC) $(if $(LLM_BLEND),--llm-blend $(LLM_BLEND),)
# 投票発火の専用デーモン (watch-auto の poll とは独立に締切 BET_LEAD_SEC 秒前ちょうどに撃つ)。
# market-blend/llm-blend は `=` 形 (bet_scheduler は = で parse)。--bet-oddspark/--bet-ipat は各 target で付与。
SCHED_ARGS := --bet-lead-sec=$(BET_LEAD_SEC) $(if $(LLM_BLEND),--llm-blend=$(LLM_BLEND),) $(if $(MARKET_BLEND),--market-blend=$(MARKET_BLEND),) $(if $(APTITUDE_TOP),--aptitude-top=$(APTITUDE_TOP),)
watch-auto:
	@echo "watch-auto: SCORE $(SCORE_WINDOW)〜$(SCORE_TOLERANCE)分 / BET $(WINDOW)±$(TOLERANCE)分 / $(INTERVAL_SEC)秒おき / Ctrl+C で終了"
	@while true; do \
		$(PY) -m src.auto_watch \
			$(BAND_ARGS) \
			--active-hours $(ACTIVE_HOURS) $(CAP_ARGS) $(BET_ARGS) || true; \
		echo "[next poll in $(INTERVAL_SEC)s]"; \
		sleep $(INTERVAL_SEC); \
	done

# --- 投票ブラウザ + watch-auto を 1 コマンドで起動 (--bet-oddspark 強制) ---
# oddspark 常駐 betting daemon (headful・起動時に人がブラウザでログイン → poll 検出) を
# background 起動しつつ watch-auto ループを回す。発走前 NAR の束が常駐ブラウザのカートに
# 積まれ続け、**購入確定は人が目視で押す** (自動では #gotobuy を押さない)。Ctrl+C で両方終了。
# SESSION_ARGS で daemon に --clear / --poll=5 等を渡せる。
SESSION_ARGS ?=
watch-auto-bet:
	@echo "watch-auto-bet: 投票ブラウザ起動(ログインしてください) + watch-auto。Ctrl+C で両方終了"
	@echo "  **購入確定は常に人。自動では #gotobuy を押しません。**"
	@bash -c 'trap "kill 0" EXIT INT TERM; \
		$(PY) -m src.oddspark_bet --session $(SESSION_ARGS) & \
		$(PY) -m src.bet_scheduler $(SCHED_ARGS) --bet-oddspark & \
		while true; do \
			$(PY) -m src.auto_watch \
				$(BAND_ARGS) \
				--active-hours $(ACTIVE_HOURS) $(CAP_ARGS) --bet-oddspark || true; \
			echo "[next poll in $(INTERVAL_SEC)s]"; \
			sleep $(INTERVAL_SEC); \
		done'

# --- JRA 即PAT 版: 投票ブラウザ(IPAT) + watch-auto を 1 コマンド (--bet-ipat 強制) ---
# ipat_bet 常駐 daemon (headful・人がブラウザでログイン → poll 検出) を background 起動しつつ
# watch-auto ループを回す。発走前 JRA の束が常駐ブラウザの購入予定リストに積まれ続け、
# **購入確定は人が目視で押す** (AUTO_PURCHASE_VERIFIED=False の間は --auto-purchase でも実弾停止)。
# 認証は env (IPAT_INETID/IPAT_SUBSCRIBER/IPAT_PARS/IPAT_PIN)。SESSION_ARGS で daemon に追加引数。
watch-auto-ipat-bet:
	@echo "watch-auto-ipat-bet: 即PAT 投票ブラウザ起動(ログインしてください) + watch-auto。Ctrl+C で両方終了"
	@echo "  **購入確定は常に人。AUTO_PURCHASE_VERIFIED=False の間は実弾を撃ちません。**"
	@bash -c 'trap "kill 0" EXIT INT TERM; \
		$(PY) -m src.ipat_bet --session $(SESSION_ARGS) & \
		$(PY) -m src.bet_scheduler $(SCHED_ARGS) --bet-ipat & \
		while true; do \
			$(PY) -m src.auto_watch \
				$(BAND_ARGS) \
				--active-hours $(ACTIVE_HOURS) $(CAP_ARGS) --bet-ipat || true; \
			echo "[next poll in $(INTERVAL_SEC)s]"; \
			sleep $(INTERVAL_SEC); \
		done'

# --- 推奨デフォルトで watch-auto-bet を起動するショートカット ---
# `make bet` 一発で 2段パイプライン:
#   SCORE 帯 5〜7分前 (Claude 考察→各馬指数) → BET 帯 1〜2.5分前 (最新オッズ+指数→束→投票),
#   INTERVAL_SEC=60, ACTIVE_HOURS=09:00-23:45, MARKET_BLEND=0.8 (市場やや寄せ),
#   MIN_PROB=0.5, APTITUDE_TOP=6, --stake-multiplier=2, 投票資金支払,
#   env 自動ログイン (ODDSPARK_ID/PASSWORD/PIN 必須), 実弾自動購入, daily_cap=¥50,000
# 個別上書き例: `make bet WINDOW=3` / `make bet LLM_BLEND=0.7` / `make bet SESSION_ARGS="..."`
bet:
	$(MAKE) watch-auto-bet \
		WINDOW=$(if $(filter command line,$(origin WINDOW)),$(WINDOW),1) \
		TOLERANCE=$(if $(filter command line,$(origin TOLERANCE)),$(TOLERANCE),1.5) \
		SCORE_WINDOW=$(if $(filter command line,$(origin SCORE_WINDOW)),$(SCORE_WINDOW),5) \
		SCORE_TOLERANCE=$(if $(filter command line,$(origin SCORE_TOLERANCE)),$(SCORE_TOLERANCE),2) \
		$(if $(filter command line,$(origin LLM_BLEND)),LLM_BLEND=$(LLM_BLEND),) \
		INTERVAL_SEC=$(if $(filter command line,$(origin INTERVAL_SEC)),$(INTERVAL_SEC),60) \
		ACTIVE_HOURS=$(if $(filter command line,$(origin ACTIVE_HOURS)),$(ACTIVE_HOURS),09:00-23:45) \
		MARKET_BLEND=$(if $(filter command line,$(origin MARKET_BLEND)),$(MARKET_BLEND),0.8) \
		MIN_PROB=$(if $(filter command line,$(origin MIN_PROB)),$(MIN_PROB),0.5) \
		APTITUDE_TOP=$(if $(filter command line,$(origin APTITUDE_TOP)),$(APTITUDE_TOP),6) \
		WITH_EXACTA=$(if $(filter command line,$(origin WITH_EXACTA)),$(WITH_EXACTA),1) \
		WITH_TRIO=$(if $(filter command line,$(origin WITH_TRIO)),$(WITH_TRIO),1) \
		SESSION_ARGS='$(if $(filter command line,$(origin SESSION_ARGS)),$(SESSION_ARGS),--auto-purchase --auto-login --clear --payment=buylimit --stake-multiplier=2 --daily-cap=50000 --poll=5)'

# --- FastAPI バックエンド ---
# keirin ev-api (8787) と完全に被らないよう keiba-ev は 9788 を既定にする。
# 「788」で keiba-ev だと識別、千の位を 9 にして keirin 8xxx 帯と物理的にずらす。
API_PORT ?= 9788
api:
	$(PY) -m uvicorn api.main:app --reload --port $(API_PORT)

# --- フロントエンド (Next.js) ---
# keirin の web (デフォルト 3000) と被らないよう 3788 を既定にする
# (web/package.json の "dev" script に `next dev -p 3788` をハードコード)。
WEB_DIR := web
WEB_PORT ?= 3788
web-install:
	cd $(WEB_DIR) && (pnpm install || npm install)

web:
	cd $(WEB_DIR) && (pnpm dev || npm run dev)
