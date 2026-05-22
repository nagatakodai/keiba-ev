.PHONY: setup setup-uv install browsers clean run run-haiku run-sonnet run-no-llm refresh verify watch watch-auto record fetch-result fetch-result-list fetch-result-process calibrate backtest bulk-fetch bulk-enum dataset train api web web-install

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
WORKERS ?= 5
RIDS_FILE ?=
bulk-enum:
	$(PY) -m src.bulk_fetch --since $(BULK_SINCE) --until $(BULK_UNTIL) --enum-only

bulk-fetch:
	$(PY) -m src.bulk_fetch --since $(BULK_SINCE) --until $(BULK_UNTIL) \
		--workers $(WORKERS) \
		$(if $(RIDS_FILE),--rids-file $(RIDS_FILE),)

# --- 学習データセット構築 (data/datasets/all.parquet) ---
LIMIT ?=
dataset:
	$(PY) -m src.dataset build $(if $(LIMIT),--limit $(LIMIT),)

# --- LightGBM lambdarank 学習 (data/models/) ---
LR ?= 0.05
ROUNDS ?= 500
LEAVES ?= 31
train:
	$(PY) -m src.train --lr $(LR) --rounds $(ROUNDS) --leaves $(LEAVES)

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

WINDOW ?= 5
TOLERANCE ?= 4
INTERVAL_SEC ?= 60
ACTIVE_HOURS ?= 09:00-23:45
watch-auto:
	@echo "watch-auto: 締切 $(WINDOW)±$(TOLERANCE) 分 / $(INTERVAL_SEC) 秒おき / Ctrl+C で終了"
	@while true; do \
		$(PY) -m src.auto_watch \
			--window $(WINDOW) --tolerance $(TOLERANCE) \
			--active-hours $(ACTIVE_HOURS) $(CAP_ARGS) || true; \
		echo "[next poll in $(INTERVAL_SEC)s]"; \
		sleep $(INTERVAL_SEC); \
	done

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
