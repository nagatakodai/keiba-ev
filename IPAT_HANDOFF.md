# 引き継ぎ資料 — JRA 即PAT(IPAT)自動投票 + Web UI 統合

最終更新: 2026-05-31 (PC再起動前). このファイルは作業継続用のメモ。再開時はまずこれを読む。

## 1. いま何をしていたか / 直近のゴール

JRA 公式 即PAT (https://www.ipat.jra.go.jp/) の **自動投票(カート投入)** を、既存のオッズパーク
投票(NAR)と同じ仕組みで実装し、**watch-auto の Web UI 開始パネルから JRA ブラウザも起動できる**
ようにした。コード実装はほぼ完了。**最後の詰まり = ブラウザがWSLg画面に表示されない問題**(下記 §5)。

## 2. 現在の到達状況(コミット済・push 済)

直近コミット(`git log --oneline`):
- `4a3db7e` 投票 daemon の Job を JOBS に登録 + Web UI に daemon ログ表示
- `122b3b5` 投票 daemon が X server/DISPLAY 不在で落ちた時に明確に案内
- `e4bdeb2` 開始ボタンでブラウザが起動しない (ループ稼働中の daemon 貼り直し)
- `0fbe997` watch-auto Web UI に「JRA 即PAT 自動投票」トグル追加 (bet_ipat)
- `c1be931` IPAT 全7式別の馬番選択を実機DOMで実装
- `ff0b236` IPAT 投票フローを実機DOMで実装 (場/レース/式別/馬番/金額/セット/購入)
- `3a49b89` / `3416d35` IPAT ログイン各段を実機DOMに修正
- `a9375b4` オッズパーク PIN 追加認証後の再ログインに対応
- `80300e1` 投票認証を .env から読む (oddspark + IPAT、load_dotenv)
- `0348474` netkeiba block 解除を polling して取りこぼし race を再取得する helper

### できていること(実機 DOM 検証済 2026-05-31)
- **IPAT ログイン全段**: INET-ID → 加入者情報(加入者番号/暗証番号/P-ARS) → メニュー。自動ログイン
  (`--auto-login`, env 認証)で「ログイン完了」まで到達することをライブログで確認済み。
- **IPAT 投票フロー全7式別**: 場 → R → 式別 select → 馬番 → 金額 → セット → 購入予定リスト →
  合計金額入力 → 購入する。馬番選択は2系統(下記 §4)。
- **Web UI 統合**: watch-auto 開始パネルに「JRA 即PAT 自動投票」トグル(bet_oddspark と独立 ON 可)。
  開始すると `ipat_bet --session` daemon が別 Job で起動。状態 `ipat_bet_running` / ログCard 表示。
- **daemon ログ可視化**: 投票 daemon(oddspark/ipat)の Job を `JOBS` に登録 → Web UI watch-auto
  ページ下部「投票ブラウザ daemon ログ」Card で各 daemon のログを stream 表示。

### 安全フラグ(重要・変更しないこと)
- **`src/ipat_bet.py: AUTO_PURCHASE_VERIFIED = False`** ← IPAT は購入確定の**最終遷移と完了画面
  (受付番号が出る画面)が未検証**なので実弾を撃たない fail-safe。実機で1回購入完了画面のDOMを
  確認したら True にできる(それまで `--auto-purchase` でもカート投入止まり)。
- `src/ipat_bet.py: _ORDERED_BETS_VERIFIED = True`(馬単/3連単 radio 確認済)。
- `src/oddspark_bet.py: AUTO_PURCHASE_VERIFIED = True`(オッズパークは実機検証済 = **実弾が出る**)。
  → オッズパークを `--auto-purchase` で動かすと**本当に買う**。daily_cap で日次上限ガード。

## 3. 主要ファイル

| ファイル | 役割 |
|---|---|
| `src/ipat_bet.py` | IPAT 投票本体(BettingSession/run_session/SELECTORS/全7式別)。`oddspark_bet.py` の JRA 版 |
| `src/oddspark_bet.py` | オッズパーク投票本体(参考実装) |
| `src/auto_watch.py` | `--bet-ipat` / `_enqueue_ipat_bet`(JRA 束を ipat_bet_queue へ) |
| `api/runner.py` | `WatchAutoManager`: `_start_ipat_daemon` / `ipat_bet_job` / `ipat_bet_running` / registry 配線 |
| `api/main.py` | `WatchAutoStartRequest.bet_ipat`、status の `ipat_bet_running`/`ipat_bet_job`、`WATCH=WatchAutoManager(registry=JOBS)` |
| `web/app/watch-auto/page.tsx` | betIpat トグル + 「投票ブラウザ daemon ログ」Card |
| `web/lib/api.ts` | WatchAutoStatus/startWatch に bet_ipat/ipat_bet_running/ipat_bet_job |
| `.env`(gitignore済) | `IPAT_INETID/IPAT_SUBSCRIBER/IPAT_PARS/IPAT_PIN`、`ODDS_PARK_ID/PASSWORD/PIN` |
| `data/cache/ipat_bet_queue/` | watch-auto → ipat daemon の受け渡しキュー |
| `data/cache/watch_auto_state.json` | watch-auto の should_run/config 永続(resume 用) |
| `data/cache/ipat_step_*.png` | IPAT 各ステップのスクショ |

## 4. IPAT 馬番選択の DOM(全7式別・実装済み)
- **単一列 checkbox `#no{N}`**(単勝/複勝/馬連/ワイド/3連複, `_SINGLE_COLUMN_BETS`):
  対象馬を check するだけ(馬連/ワイド=2頭で1組、3連複=3頭で1組)。方式 select は「通常」。
- **着順列 radio `#horse{pos}_no{N}`**(馬単=1着/2着 の2列、3連単=1着/2着/3着 の3列, `_ORDERED_BETS`):
  key 順に着順を選ぶ。
- 式別 select = `select#bet-basic-type`(option **ラベル**で選択。3連複/3連単は**全角**「３連複」「３連単」)。
- 金額 = `.selection-amount input[ng-model="vm.nUnit"]`(100円単位)。セット = `button[ng-click="vm.onSet()"]`。
- 入力終了 = `button[ng-click="vm.onShowBetList()"]`。合計金額入力 = `input[ng-model="vm.cAmountTotal"]`。
  購入する = `button[ng-click="vm.clickPurchase()"]`。
- IPAT は **AngularJS SPA**(ui-router, hash route `#!/bet/basic`)。ログイン判定 = `text=ログアウト`。

## 5. ★未解決の問題(再起動後に最優先で切り分け)★

**症状**: Web UI(make api)から開始すると、daemon ログ上は IPAT が「ログイン完了。queue 監視開始」、
オッズパークも「常駐セッション開始」まで進み、`ipat_bet_running=true`。**= chromium は起動して
Playwright が操作できている(自動ログイン成功)**。にもかかわらず **ブラウザのウィンドウが画面に
出てこない**(両方とも)。X server エラーは出ていない(`DISPLAY=:0` で auth も通っている)。

**重要な観察**:
- **直接 `.venv/bin/python -m src.ipat_bet --session` を端末で実行した時はブラウザが見えていた**
  (実際にDOMを採取できた)。
- **make api 経由だと見えない**。→ 同一マシンでも **make api を起動したセッション/コンテキストの
  表示環境**が、直接実行した端末と違う(DISPLAY=:0 でも WSLg の実画面に繋がっていない)のが最有力。

**再起動後の切り分け手順**:
1. `make api` を起動する**その端末**で `echo $DISPLAY`(:0 のはず)。
2. 同じ端末で最小ブラウザ表示テスト:
   ```bash
   cd ~/keiba-ev
   .venv/bin/python -c "from playwright.sync_api import sync_playwright as s; p=s().start(); b=p.chromium.launch(headless=False); b.new_page().goto('about:blank'); __import__('time').sleep(15)"
   ```
   - **ウィンドウが出る** → 表示環境OK。make api 経由も出るはず(最小化/別ウィンドウを alt-tab で確認)。
     → それでも見えないなら make api 子プロセスの env(XDG_RUNTIME_DIR/XAUTHORITY 等)を比較する。
   - **出ない** → その端末では表示できない。**直接実行でブラウザが出た端末と同じ場所で `make api` を
     起動し直す**(nohup/tmux/screen/SSH再接続後だと WSLg に繋がらないことがある)。
3. 確実な回避策(make api を介さない):表示OKの端末で
   ```bash
   make watch-auto-ipat-bet   # JRA(IPAT) ブラウザ + watch-auto を1コマンド
   ```
4. make api 子の実 env を確認するなら:`cat /proc/<make api の uvicorn worker PID>/environ | tr '\0' '\n' | grep -E 'DISPLAY|XAUTH|XDG_RUNTIME|WAYLAND'`

**再起動で直る可能性**: WSLg のセッション/X authority がリセットされ、新しい WSLg 端末から普通に
`make api`(または `make watch-auto-ipat-bet`)を起動すれば表示される見込み。

## 6. 起動コマンドまとめ

```bash
# Web UI(API+フロント)。**DISPLAY のある WSLg 端末で起動すること**(headful ブラウザ表示のため)
make api      # uvicorn :9788 (--reload 既定。コード変更で daemon 再起動→再ログイン要)
make web      # next :3788

# CLI で直接(端末の表示環境をそのまま使うので確実):
make watch-auto-ipat-bet            # JRA(IPAT) daemon + watch-auto(--bet-ipat 強制)
make watch-auto-bet                 # オッズパーク daemon + watch-auto(--bet-oddspark 強制)
.venv/bin/python -m src.ipat_bet --session [--auto-login] [--auto-purchase]  # IPAT daemon 単体
# ↑ python ではなく必ず .venv/bin/python(`python` は未インストール)

# watch-auto のみ(ブラウザ無し・キュー投入だけ):
make watch-auto BET_IPAT=1
```

## 7. 注意点 / ハマりどころ
- **`python` コマンドは無い**。必ず `.venv/bin/python` か `make` 経由。
- **make api は DISPLAY のある WSLg 端末で起動**(無いと headful 起動失敗)。
- `watch_auto_state.json` に `should_run:true` が残っていると、make api 起動時に **resume が前回 config
  で自動的にループ+daemon を起動**する。挙動が変なときは一度 Web UI で「停止」するか、このファイルを消す。
- オッズパークの `--auto-purchase` は **実弾**(AUTO_PURCHASE_VERIFIED=True)。表示が見えないまま動かすと
  キュー投入で勝手に買う。検証中は「停止」推奨。
- IPAT は AUTO_PURCHASE_VERIFIED=False なので実弾は出ない(カート投入まで)。

## 8. IPAT を実弾全自動まで仕上げる残タスク(任意)
1. 実際に1点(複勝など)を**手動で購入確定**し、**購入完了画面(受付番号が出る画面)のDOM**を採取。
2. `src/ipat_bet.py` の `_confirm_purchase` の success marker / 最終ボタンセレクタを実DOMで確定。
3. `AUTO_PURCHASE_VERIFIED = True` に変更。
4. ※ IPAT は**事前入金(チャージ)必須**。未入金だと購入は最終段で弾かれる(`_dismiss_deposit_dialog`
   が入金案内を閉じるが資金が無いと買えない)。

## 9. 別件(進行中の可能性)
- `scripts/_retry_when_unblocked.py`: netkeiba block 解除を polling して取りこぼし 143 race を
  `data/cache/rids_retry.txt` から再取得する常駐スクリプト。PC再起動で**プロセスは死ぬ**ので、
  まだ取れていなければ再実行: `.venv/bin/python -m src.bulk_fetch --rids-file data/cache/rids_retry.txt --workers 2 --polite-ms 2000`
  (netkeiba block 解除後に)。
