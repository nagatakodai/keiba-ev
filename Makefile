.PHONY: setup setup-uv install browsers clean run run-haiku run-sonnet run-no-llm refresh verify watch watch-auto record fetch-result fetch-result-list fetch-result-process calibrate api web web-install

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
# 使い方: make run URL='https://www.winticket.jp/keiba/<venue>/...' [EV_MAX=3] [MIN_PROB=2.0] [MARKET_BLEND=0.6]
EV_MAX ?=
MIN_PROB ?=
MARKET_BLEND ?=
CAP_ARGS := $(if $(EV_MAX),--ev-max $(EV_MAX),) $(if $(MIN_PROB),--min-prob $(MIN_PROB),) $(if $(MARKET_BLEND),--market-blend $(MARKET_BLEND),)

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
ACTIVE_HOURS ?= 09:30-17:30
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
# keirin ev-api がデフォルトで 8787 を掴んでいるので、keiba-ev は 8788 を既定にする。
API_PORT ?= 8788
api:
	$(PY) -m uvicorn api.main:app --reload --port $(API_PORT)

# --- フロントエンド (Next.js) ---
WEB_DIR := web
web-install:
	cd $(WEB_DIR) && (pnpm install || npm install)

web:
	cd $(WEB_DIR) && (pnpm dev || npm run dev)
