# keiba-ev — 中央競馬 EV 分析プロジェクト

## 開発フロー

- **commit + main push は確認なしで OK** (2026-05-29 ユーザ許可)。論理的なまとまりごとに commit して main へ直 push してよい。
- 但し依然として要確認: 破壊的操作 (reset --hard / force push / branch -D), `.env` 等機密のコミット可能性, hooks の skip。
- commit message は日本語 conventional 風 (`feat:` / `fix:` / `refactor:` 等)。Co-Authored-By 付け。


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
| **www.jra.go.jp (JRA 公式)** | ✅ 200 | **JRA オッズ源** (`src/scrape_jra.py`)。`accessO.html`/`accessS.html` への form POST チェーン (Shift_JIS) を doAction トークン抽出で walk。**全7券種 + 着順 + 払戻が組合せ明示**。oddspark の位置推定問題は無く全券種採用可。当日/直近開催のみ。発走前 live odds 更新挙動は開催日に要確認 |
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

**NAR は keiba.go.jp で完全自給 (`src/scrape_keibago.py`)**: オッズ・出馬表・馬柱すべてを地方競馬公式の静的 HTML から取得し、netkeiba/oddspark 非依存で確率モデルをフル稼働できる。
- **オッズ (全6券種, 組合せ明示)**: netkeiba は NAR の馬連/ワイドが壊れ、oddspark はグリッド誤オッズで馬連/ワイド/馬単/3連複が無効。keiba.go.jp は全6券種が組合せ明示 (`<td>6-7-11</td>`) なので誤オッズ無しで馬連/ワイド/馬単/3連複/3連単すべてを復活できる。
- **出馬表 (`DebaTable`)**: 馬番/馬名 + 各馬の競走馬 ID (`k_lineageLoginCode`) を取得 (`parse_deba_table`)。
- **馬柱 (`DataRoom/HorseMarkInfo?k_lineageLoginCode=<ID>`)**: 競走成績 (日付/競馬場/距離/馬場/頭数/着順/タイム) を `parse_horse_history` でパース → `PastRun` 構築。netkeiba 馬柱 (5走) より長い履歴。**leakage 防止**: 対象 race 日付以降を除外 (`_date_key` でタプル比較) + 直近5走に制限。live (発走前) では no-op。
- `find_keibago_race`: netkeiba NAR race_id → 場名(`VENUE_CODE`) → babaCode を `TodayRaceInfoTop` から**動的照合** (別 namespace の babaCode 誤りで別場を取らない安全策)。
- `analyze_keibago`: cache 出馬表があればそれを優先、無ければ DebaTable + HorseMarkInfo で出馬表+馬柱を構築 (公式自給) → `build_features`/`aptitude`/`estimate_probs` が edge を反映。DebaTable も取れなければ単複の馬リスト+市場ブレンド主導に degrade。
- **安全ゲート**: `check_consistency` で「ワイド>馬連」等を検知したら pair/trio 系を drop (誤オッズより見送り)。
- `python -m src.scrape_keibago <netkeiba_nar_rid> [--snapshot]`。TodayRaceInfo なので当日向け。
- **JRA は keiba.go.jp 非対応** → JRA 公式 (`src/scrape_jra.py`) で自給 (下記)。

**JRA は JRA 公式 (`src/scrape_jra.py`) で自給**: netkeiba と独立した公式オッズ/結果源。`accessO.html` (オッズ) / `accessS.html` (結果) への form POST チェーン (Shift_JIS, cookie 不要, GET は 301) を **doAction(...,'token') 抽出で walk** する。token は checksum 必須で推測不可なので各段 HTML から抽出して次段 POST。
- **オッズ (全7券種, 組合せ明示)**: 単勝/複勝/馬連/ワイド/馬単/3連複/3連単。馬連/ワイド/馬単 = `<caption>軸</caption>` + `<th scope="row">相手</th>` + `<td>odds</td>` (相手明示・位置推定不要)、ワイドは min。3連複 = `<caption>a-b</caption>` + th(3頭目)。3連単 = `sub_header`(1着) + 2着区切り + th(3着)、**1 POST で全 ordered triple** (oddspark の軸別 GET 不要)。oddspark で無効化した馬連/ワイド/馬単/3連複も **JRA なら位置推定不要で全採用**。
- **token ↔ netkeiba JRA race_id**: 段1 `pw15orl1<vv><yyyy><kk><dd><date>`、段2 `pw15<bt>ou1<vv><yyyy><kk><dd><RR><date>`。`find_jra_race` が netkeiba rid (YYYY VV KK DD RR) を venue+kai+day+RR で walk して各券種 token を集める。`discover_jra_races` で直近開催を列挙。
- **結果**: `fetch_jra_result` が `accessS` を walk (段2 結果 token `pw01sde1...` は doAction 形式でないので raw 抽出) → `td.place`+`td.num` の着順 + `li.tierce` の3連単配当。
- **analyze_jra**: cache に netkeiba 出馬表/馬柱があれば確率モデルがフル稼働、無ければ単勝の馬リストで市場主導 (JRA 公式馬柱=accessU パースは未実装の宿題)。snapshot は `odds_source="jra"`。CLI `python -m src.scrape_jra <rid> [--snapshot]` / `--discover`。
  - **馬名/騎手/性齢/馬体重/斤量 は accessO 単複ページから取得** (`parse_jra_horses`, 実機確認 2026-05-31)。単複オッズ表の各行は 馬名(`td.horse`の accessU リンク=horse_id)・性齢(`td.age`)・馬体重±増減(`td.h_weight`)・斤量(`td.weight`)・騎手(`td.jockey`)・調教師(`td.trainer`) を持つ。
  - **レース条件 (距離/馬場/クラス) も単複ページから** (`parse_jra_race_header`): `コース： 1,400 メートル （ダート・左）` + `cell category/class` (例 3歳+未勝利) → Race.distance/surface/race_class。Claude 考察の「距離・馬場欠損」が解消し distance/surface_fit も効く。
  - **馬柱 (past_runs) は accessU から取得** (`parse_jra_past_runs`/`fetch_jra_past_runs`, 実機確認 2026-05-31)。馬詳細ページ 1 番目の `table.basic.narrow-xy.striped` (年月日/場/レース名/距離/馬場/頭数/人気/着順/騎手/負担重量/馬体重/タイム) を PastRun 化 (タイム=自走時計→winner_time_sec+diff0、着順 1/2/3 のみ int)。`build_jra_racedata(..., fetch_past=True, target_date=...)` が **score 帯で馬ごとに accessU を引いて `data/cache/jra_pastruns/<rid>.json` にキャッシュ**、bet 帯は cache を読むだけ (締切直前の latency 回避)。leakage は target_date 以降を除外+直近5走。→ **netkeiba cache 無しの JRA でも確率モデルがほぼフル稼働** (上3F/通過順は accessU に無いのでその特徴のみ欠落)。
- **result fetch 統合**: `fetch_result.process_pending` は netkeiba block 失敗時、NAR→keiba.go.jp / **JRA→`fetch_jra_result`** で確定結果を fallback 取得して save (block 中も loop が閉じる)。実機検証: 東京/京都/新潟 12R の着順+3連単配当を取得、process_pending 経由で fallback→success 確認。
- **宿題**: ①発走前 live odds の更新挙動 (開催日に要実機確認、確定版と同構造の見込み) ②(済) JRA 公式馬柱 (accessU) パース — 実装済 ③watch-auto の block 中 JRA 自動 predict-dispatch (JRA 発走時刻ソースが必要)。

**オッズパーク投票 (`src/oddspark_bet.py`)**: 既定は **半自動 (B案)** = 3連単束 (`recommended_bundle_t.legs`, 3連単的中モード固定) を**カートに投入するところまで** Playwright で自動化し、**購入確定は人が headful ブラウザで目視して押す**。`--auto-purchase` で**全自動 (実弾) モード** に切替え、#gotobuy → 投票申込確認画面 (`VoteConfirmOpcoin.do`) → **`#buy` (`<a id="buy" onclick="clickVoteComplete()">投票を申込</a>`)** → `VoteCompleteOpcoin.do` で確定 POST まで人の介入なしで実行する (実機 HTML 確認済 2026-05-28、`AUTO_PURCHASE_VERIFIED=True`)。安全策は四段構え: ①`max_total_stake` per-race (優先順: `--max-stake=N` 円で明示 > `--max-stake-multiplier=N` 上限専用倍率 (基準¥10,000×N、Web UI の「per-race 上限倍率」から指定可・掛金倍率と独立) > `--stake-multiplier` に連動して 基準¥10,000×掛金倍率 へ自動スケール = 倍率を上げても全 race reject されない。例: 倍率2→上限¥20,000) ②`daily_cap` 日次累計上限 (`--daily-cap=50000`、JST 日跨ぎで自動 reset、`data/cache/oddspark_daily_stake.json` に永続) ③`AUTO_PURCHASE_VERIFIED` 実機検証フラグ (True なら実弾可、緊急時に False に戻せば fail-safe で実弾停止) ④success marker (受付完了/投票完了 等のテキスト) を必ず検出してから daily_stake を加算 (誤検知/失敗で二重購入しない)。認証は **`.env`/環境変数のみ** (`ODDS_PARK_ID`/`ODDS_PARK_PASSWORD`/`ODDS_PARK_PIN`、旧 `ODDSPARK_*` も fallback。`load_dotenv` で `.env` から読む、`.env` は .gitignore 済)、コード/ログ/コミットに残さない。`--manual-login` で認証情報を使わず人が手でログイン (最も安全)。オッズパーク利用規約による自動化制限のリスクは使用者が負う。`python -m src.oddspark_bet <netkeiba_nar_rid|race_id>` (netkeiba 12桁 rid・内部 race_id `<cup>-<si>-<rn>` どちらも可)。
- **実機検証済 (2026-05-27 園田9R, --manual-login)**: 手動ログイン→投票TOP→レースまとめ投票→OPコイン選択→既存カート全削除→各脚セット→**人が確定**、の全フローが動作。8脚 (ワイド4+馬連4) が組数8通り/¥2,600 で `#buylist` に正しく投入されることを確認。
- **確定セレクタ (まとめ投票画面)**: `matome_link=#todayMultiRace` / レース checkbox `value=<YYYYMMDD>_<joCode>_<raceNo非ゼロ詰め>` / `betTypeSelect` value=1単勝2複勝5馬連6馬単7ワイド8三連複9三連単 / 金額 `#textfield11` (100円単位) / セット `#multiSet` / 支払OPコイン `#paymentMethodOpCoin` / 馬番グリッド `#horseArea td[name="horse1|2|3"] a` (着順列, text=馬番, 選択時 `class="on"`) / **馬番リセット `#reset`** / 全買い目削除 `#all a` / 購入確定 `#gotobuy` (**絶対に押さない**)。投票 joCode は `VOTE_JO_CODE` (場名→値, オッズ側 opTrackCd とは別 namespace)。
- **致命的バグだった「馬番グリッド累積」(修正済)**: まとめ画面のグリッドは セット しても馬番選択が残るため、**脚ごとに `#reset` でクリアしないと**次脚の click が累積し、共有馬番 (例: 3連系の軸) の再 click は `on` をトグル OFF にして組番が壊れる (実機: 馬連が「フォーメーション3通り」化、ワイド重複/欠落、組数10通り/¥3,000 に誤膨張)。対策: ①各脚の馬番選択前に `_reset_umaban` (`#reset` → フォールバックで text/JS) ②セット直前に `#horseArea td a.on` 数 == 馬番数 を検証し不一致なら当該脚を中止 (壊れた組番を commit しない) ③開始時に `#all a` で既存カートを削除。confirm ダイアログは `page.on("dialog", accept)` で自動承認 (購入確定は押さないので削除確認のみに作用)。
- 順不同 (馬連/ワイド/3連複) は着順列に各1頭ずつ置けば1組 = 1通り。馬単/3連単は key 順に 1/2/3着列。各 step で `data/cache/oddspark_step_*.png` にスクショ。エラー時もブラウザは閉じず開いたまま (調査用)、最後に人が `#buylist` を確認して Enter。
- **馬単/3連単 (順序付き) も自動投入可** (`_ORDERED_BETS_VERIFIED=True`, 実機検証済 2026-05-28 笠松1R)。実機 DOM で判明: まとめ画面の 裏目/マルチ は **checkbox ではなく `<a id="betType{4,6,9}MultiFlag" class="btn-urame|btn-multi">` のトグルリンク**で、betType checkbox を選ぶと `disabled`→enabled になるが **ON クラスは付かず既定 OFF**。馬単[1,2]→組数+1、3連単[1,2,3]→組数+1 で、#buylist は `1→2` / `1→2→3` の正順 (= 単一順列、全順列化されない)。安全は二段構え: ①`_uncheck_ura_multi` が `a[id$=MultiFlag]` の有効化された ON トグルを click で OFF (既定 OFF なので通常 no-op) ②`_assert_combo_delta` がセット後に **組数が +1 だけ増えたか**検証し、+2/+6 (裏目/マルチ) や馬番累積を捕捉して当該脚を中止 (組数を読めない順序付きも安全側で中止)。`_combo_count` は '組数：N通り' を body テキストから要素 id 非依存で読む。レース選択 checkbox は `name="group1/group2"` value=`YYYYMMDD_joCode_raceNo`。検証は `scripts/verify_oddspark_ordered.py` (persistent profile でログイン保持・`--set` で試投入・`--clear` でカート全削除、**#gotobuy は押さない**)。

**JRA 即PAT 投票 (`src/ipat_bet.py`)**: oddspark (NAR) の対になる **JRA 公式 IPAT 自動投票**。同じ設計 (env 認証 / SELECTORS 集約 / `BettingSession`+`run_session` daemon / queue / 四段安全ゲート / watch-auto 連携) を JRA 用に移植した scaffold。**JRA レース (venue code 01-10) のみ**を対象 (`_is_jra_rid`)、NAR は oddspark 経路へ。queue は `data/cache/ipat_bet_queue/` (oddspark とは別 namespace)、daily_stake は `data/cache/ipat_daily_stake.json` で独立管理。認証は **`.env`/環境変数のみ** (`load_dotenv`): `IPAT_INETID` / `IPAT_SUBSCRIBER` (加入者番号) / `IPAT_PARS` (P-ARS番号) / `IPAT_PIN` (暗証番号)。既定は半自動 (カート投入まで自動・**購入確定は人**)、`--auto-purchase` で全自動。`python -m src.ipat_bet <netkeiba_jra_rid|race_id>` (one-shot) / `--session` (daemon)。watch-auto は `--bet-ipat` で発走前 JRA の束を enqueue (`_enqueue_ipat_bet`)。Makefile: `make watch-auto BET_IPAT=1` または `make watch-auto-ipat-bet` (daemon+ループ 1 コマンド)。
- **実機 DOM 確認状況 (2026-05-31)**: ログイン〜投票画面〜購入予定リストまで**実機 DOM 取得済**で `SELECTORS` を実値化した。IPAT は **AngularJS SPA** (ui-router, hash route `#!/bet/basic`)。各ステップで `data/cache/ipat_step_*.png` にスクショ。
  - **ログイン (確定)**: 段1 INET-ID (`input[name=inetid]` + `<a onclick=send()>`)、段2 加入者情報 (`name=i` 加入者番号 / `name=p` 暗証番号 / `name=r` P-ARS、送信 `<a onclick=ToModernMenu()>`)。端末登録後にログイン画面へ戻される挙動は再ログインで突破 (oddspark 同様)。ログイン判定 `text=ログアウト` (`a[ui-sref=logout]`)。
  - **投票 (確定)**: 通常投票 `button[ui-sref=bet.basic]` → 場 `.places button`(has-text 場名) → R `.races button`(R 完全一致) → 式別 `select#bet-basic-type` (option ラベルで選択, ３連複/３連単は**全角３**) → 馬番チェック `#no{N}` → 金額 `.selection-amount input[ng-model=vm.nUnit]`(100円単位) → セット `button[ng-click=vm.onSet()]` → 入力終了 `button[ng-click=vm.onShowBetList()]` → 購入予定リスト → 合計金額入力 `input[ng-model=vm.cAmountTotal]` → 購入する `button[ng-click=vm.clickPurchase()]`。
- **対応式別 (全7券種, 実機 DOM 確認済 2026-05-31)**: 馬番選択は2系統:
  - **単一列 checkbox `#no{N}`** (単勝/複勝/馬連/ワイド/3連複, `_SINGLE_COLUMN_BETS`): 選んだ馬だけ check (馬連/ワイド=2頭で1組、3連複=3頭で1組)。方式=通常。
  - **着順列 radio `#horse{pos}_no{N}`** (馬単=2列/3連単=3列, `_ORDERED_BETS`): key 順に 1着/2着(/3着) を選択。方式=通常 (`_ORDERED_BETS_VERIFIED=True`)。
  - 各 leg で式別 select → 方式=通常 (前 leg の ながし/ボックス を正す) → 馬番 → 金額 → セット。
- **安全フラグ**: **`AUTO_PURCHASE_VERIFIED=True`** (2026-05-31 実機検証済 — 購入する `vm.clickPurchase()` → 確認ダイアログ `error-window`「投票内容と金額を送信してもよろしいですか？」OK=`button.btn-ok[ng-click=vm.dismiss()]` → 投票結果画面「お客様の投票を受け付けました。/ 受付番号：NNNN」まで実 DOM で確認)。`--auto-purchase` で実弾、緊急時に False へ戻せば fail-safe で実弾停止。success 後は「続けて投票する」(`vm.clickContinue()`) で bet.basic へ戻し次レースを受ける。`_ORDERED_BETS_VERIFIED=True` (馬単/3連単 radio 確認済)。
- **馬番選択は直 `.check()` 禁止 (重要)**: 単一列 checkbox (`#no{N}`) も着順列 radio (`#horse{pos}_no{N}`) も `<label><input(CSS不可視)><span class=check(視覚要素)></label>` 構造で、input を直接 `.check()` すると `span.check` (行が画面上端だと固定 navbar の `h1`) がクリックを intercept して Timeout → 買い目が積まれない (実機: ワイドで再現 2026-05-31、3連単 脚1 Timeout 30000ms で再発 2026-06-06)。→ checkbox は `_check_horse_box` (`span.check`→`label`→force)、radio は `_check_horse_radio` (`span.check`→`label`→`dispatch_event("click")`) の三段 + 事前に `scrollIntoView({block:'center'})` + `is_checked()` 確認。radio はクリックで解除できず同馬が他列選択中だと ng-disabled になるため、leg 失敗の残骸と矛盾する場合は式別を選び直して再描画クリア (`_ordered_columns_dirty`)、クリアできなければ誤組番防止で当該脚を中止。
- **⚠ 事前入金 (チャージ) が必須**: 即PAT は購入限度額 0 円だと投票不可。未入金だとレース選択/購入時に「投票の前に入金してください」ダイアログ (`_dismiss_deposit_dialog` が『このまま進む』で閉じるが、資金が無ければ最終購入は弾かれる)。運用前に手で入金しておく。

**watch-auto との連携 (常駐セッション + キュー)**: watch-auto は `_watch_loop.py` が interval 毎に `auto_watch.main()` を**毎回フレッシュ subprocess** で起動する構造なので、ブラウザを巡をまたいで保持できない。そこで **ログイン済みの持続ブラウザを別プロセス (`BettingSession`/`run_session`) で持ち**、watch-auto は queue 経由で「このレースを入れて」と渡す:
- **常駐 daemon**: `python -m src.oddspark_bet --session [--auto-purchase] [--daily-cap=50000]` → headful ブラウザ起動 → **人が手でログイン (起動時1回)** → まとめ画面で待機し `QUEUE_DIR` (`data/cache/oddspark_bet_queue/`) を poll。新規 `<netkeiba_rid>.req` を見つけたら snapshot の 3連単束 (`recommended_bundle_t.legs`) を**同じブラウザにカート投入** (処理後 `.done` に rename して再投入防止)。Claude 指数なし (`rank_source != "claude"`) の束は投入しない。`add_race` は **対象レースのみ checkbox を選択し他レースは uncheck** (複数レース同時セットの誤発注防止)、脚ごとに `#reset`→馬番→`a.on`数検証→セット。`--auto-login` で env 認証、`--poll=N` で間隔、`--clear` で開始時カート全削除。`--auto-purchase` 無しで既定の半自動 (人が確定)、有りで実弾 (上記安全策付き)。Ctrl-C で終了。
- **enqueue**: `auto_watch --bet-oddspark` で、**締切 N 分前 dispatch** (--window 5 既定 = 締切5分前 = 発走7分前)が `rc==0` かつ snapshot に**束(legs)が非空**なら `_enqueue_oddspark_bet` が `QUEUE_DIR/<netkeiba_rid>.req` を atomic 書き込み。**NAR (投票 joCode がある場) のみ・見送り(束空)は投入しない・未投入のみ** (JRA は oddspark 投票非対応なので skip)。
- 運用 (2端末): 端末A `python -m src.oddspark_bet --session` (ログインして放置) / 端末B `make watch-auto BET_ODDSPARK=1` (または `python -m src.auto_watch --bet-oddspark ...`) → 発走前 NAR の束が常駐ブラウザのカートに積まれ続け、人が目視で確定。`BettingSession` は one-shot `fill_cart` とも共用 (one-shot は1レース→input待ち→close、daemon は queue ループ)。
- 運用 (Web UI): `make api` + `make web` の watch-auto ページ開始パネルに **「オッズパーク自動投票(カート投入)」トグル** (`bet_oddspark`)。ON で開始すると API (`WatchAutoManager`) が ①auto_watch に `--bet-oddspark` 付与 ②投票 daemon (`oddspark_bet --session`) を別 Job として起動 (headful・poll ログイン)。**ブラウザは uvicorn の env を継承するので `make api` を DISPLAY のある端末 (WSLg 等) で起動しておくこと** (無いと headful 起動に失敗 → ライブログ/「投票ブラウザ未起動」表示)。watch-auto 停止で daemon も停止。daemon は `_ALIVE_JOBS`+PDEATHSIG で uvicorn 終了時も道連れ。status は `bet_running`/`bet_job` を返す。**uvicorn --reload (make api 既定) は code 変更毎に daemon を再起動→ブラウザ再ログインが要る**点に注意。購入確定は常に人。
- 運用 (1コマンド統合 CLI): `make watch-auto-bet` で **daemon (headful ブラウザ起動→人がログイン) + watch-auto ループ (--bet-oddspark 強制) を1コマンド**起動。`bash -c 'trap "kill 0" ...'` で daemon を `&` 背景起動しつつループを回し、Ctrl+C で両方終了。daemon のログインは **poll 検出** (`BettingSession._wait_manual_login`, logged_in_marker を最大 `login_wait_sec`=600s polling) なので `input()` 非依存で背景起動に耐える (`watch-auto` 本体は毎tick フレッシュ subprocess でブラウザ保持不可なので、ブラウザは依然この別 daemon プロセスが常駐保持する)。`SESSION_ARGS` で daemon に `--clear`/`--poll=5` を渡せる。**購入確定は常に人** (`#gotobuy` は自動で押さない)。
- **累計露出の注意 (重要)**: per-race ハードリミット (既定¥10,000、倍率連動) は **1レース分の保証**であって `#buylist` 全体の総額ではない。**購入確定は #buylist 全体を一括購入する**ので、daemon が複数レースを溜めた状態で確定すると全部買う。→ **レースごとに確定/クリアする**のが基本。`BettingSession` は `_session_staked` (本セッション投入累計, 未確定含む) を表示し、per-race 上限を超えたら警告する (確定を検知できないので累計はリセットされない=溜め過ぎ検知用)。
- **二重投入防止 (重要)**: `<rid>.done` は **再 enqueue/再投入のガード**なので消さない (`_enqueue_oddspark_bet` が `req.exists() or done.exists()` で弾く + watch-auto の analyzed.txt dedup の二段)。`.done` を prune すると analyzed が消えた場合に二重ベットし得るため永続させる。

**投票フラグによる予想対象の絞り込み (ユーザ指示 2026-06-13)**: watch-auto は **JRA 自動投票 (`--bet-ipat`) だけ ON なら地方 (NAR) を予想しない / oddspark 自動投票 (`--bet-oddspark`) だけ ON なら JRA を予想しない**。`auto_watch._race_type_predict_enabled(source, bet_oddspark, bet_ipat)` が判定し、`_run_phase` (score 帯 dispatch + bet 予約プリパス) の `due`/`future_all` と `_fire_due_bets` (予約発火) の両方で対象外券種を skip する (= score も bet も予想しない → 賭けない券種に LLM/検索/解析コストを使わない)。`source=="keibabook"` が JRA・それ以外 (oddspark/keibago) が NAR で投票 routing と同 signal。**両方 ON / 両方 OFF (計測のみ) は両方予想** (絞り込みなし)。Web UI のトグルは API が `--bet-ipat`/`--bet-oddspark` に変換して伝播するので UI 操作でも効く。

**oddspark 非対応場 (南関東/門別) を keiba.go.jp で discovery 補完 (ユーザ指示 2026-06-30)**: oddspark は **南関東 (大井/船橋/川崎/浦和) や門別 を売らない**ため `fetch_race_list_oddspark` の discovery から漏れ、それらの場が予測対象外になっていた。`scrape_keibago.fetch_race_list_keibago(yyyymmdd, skip_venues)` が **TodayRaceInfoTop → 各場 RaceList** をパースして当日全 NAR 場の race_no + 発走時刻 + netkeiba_rid を構築し (RaceList は「発走時刻(HH:MM)→k_raceNo」の順で並ぶので直前時刻を初出 race_no に紐付け)、`discover_today_races` が **oddspark で取れた場を skip_venues で除いた残り** (=oddspark 非対応場) を `source="keibago"` で追加する。keiba.go.jp は 大井/門別 の **オッズも出す** (実機: 大井12頭/門別8頭の単勝取得確認) ので、discovery→odds→`_dispatch_nar_fallback`(keibago優先)→snapshot→計測 まで他 NAR と同じ pipeline に乗る。`tests/test_scrape_keibago.py::test_fetch_race_list_keibago_discovers_nankan`。

**watch-auto への統合 (block 中も自動継続)**: netkeiba 両ドメイン block 時、`auto_watch._list_due_races` は **oddspark で NAR race discovery** (`fetch_race_list_oddspark`: KaisaiRaceList→OneDayRaceList で当日全 NAR の race_id + 発走時刻) に fallback し、該当レースは `source="oddspark"` を付けて `_dispatch_nar_fallback` で dispatch する → **keiba.go.jp を優先** (`_dispatch_keibago`: `python -m src.scrape_keibago <rid> --snapshot --start-at=<unix>`、全6券種)、keiba.go.jp が解決できない場のみ **oddspark にフォールバック** (`_dispatch_oddspark`、単複/3連単)。トリガミ防止束を含む snapshot を保存 (`odds_source="keibago"`/`"oddspark"`, 発走時刻も補完)。これで規制中でも NAR の watch-auto が止まらず 3連単的中モードの束を出し続ける (keibalab は当日一覧が JS 化で discovery 不能なため最終 fallback)。claude 考察 (score ステージ各馬指数 + 3連単買い目選定) も oddspark 経路を含む全経路で netkeiba 経路と同じ関数で実行され (`--phase=score|bet`)、履歴・snapshot も同形式で作られる。

**block 中の result fetch (`fetch_result.process_pending`)**: 結果取得は第一に netkeiba。失敗理由が block (`NetkeibaBlocked`/空 body/CloudFront) で、かつ **NAR レースなら keiba.go.jp で確定結果を fallback 取得** (`scrape_keibago.fetch_keibago_result`: RaceMarkTable の着順表 + RefundMoneyList の組番一致3連単配当) → そのまま save_result。**final_odds は実払戻ベース (2026-07-04 修正)**: 旧実装はオッズページ snapshot の複勝/ワイド**レンジ下限**を実払戻として保存し的中払戻を最大 -54% 過小計上していた → keibago は `parse_refund_payouts` (RefundMoneyList 全券種・馬連複=馬連/馬連単=馬単・枠連 skip)、JRA は `parse_jra_payouts` (結果ページ li.win/place/umaren/wide/umatan/trio/tierce、追加 fetch なし) で in-money 組を実払戻 (÷100) に上書き、オッズ snapshot は lookup 被覆用の補完に格下げ。**同着 (dead heat) 対応 (2026-07-04)**: writer 3経路 (netkeiba parse_result / keibago / JRA) が着順 1-3 の全馬を `finish_positions` {馬番:着順} で保存 (2着同着=3着なしのレースは旧実装だと len<3 で永久に未確定扱いだった → positions から rank 順に finish を再構成)。reader (`api/store._finish_ranks`+rank パターン判定) が同着側の的中を公式払戻ルールどおり計上 (同着なしは従来と完全同値・旧 result は finish_order に fallback)。これで **predict (keiba.go.jp 自給) と同様に result も自給**でき、netkeiba block 中でも NAR は watch-auto→snapshot→結果取得→calibration の loop が完結する (実機確認: 金沢12R で netkeiba block→keiba.go.jp fallback→着順8-3-6/3連単6030円を save、calibrate に反映)。keiba.go.jp でも取れない (JRA / 当日外 / 未確定) ときは従来どおり **attempt を消費せず `BLOCK_RETRY_INTERVAL_SEC` (15分) 間隔で pending を維持**し解除後に取得 (block でない通常の失敗は max_attempts で failed)。`fetch_keibago_result` は当日確定レースのみ (TodayRaceInfo ベース)。

**結果の自動取得ループ (`api.main.ResultAutoFetcher`, make api 稼働中, 2026-06-20 ユーザ指示)**: 「make api 実行中は予測分析履歴の結果を取り続けて」に対応。API に常駐 asyncio ループを持たせ、既定 **10 分毎** (2026-06-28 ユーザ指示で 5→10 分・env `KEIBA_RESULT_FETCH_INTERVAL_SEC`) に ①**発走済・結果未取得の全予測** (日付不問・2026-06-28 ユーザ指示で「本日分のみ」→「全レース」に拡大、`list_predictions`) を `fetch_result.schedule(..., resurrect_failed=False)` で pending に enqueue (内部 race_id→netkeiba rid 復元・既存結果は no-op・dedup) ②`process_pending` で確定結果を取得 → `data/results` 保存 → calibrate / 予測分析履歴 / ダッシュボードに反映。**watch-auto を回していなくても** (手動 analyze / 勝負レース由来の予測も含め) 結果が埋まり続ける。発走前 (`start_at` 無し含む) は対象外なので enqueue 件数は数十程度に収まる (実測 ~39、holdout backtest は start_at 無しで除外)。`resurrect_failed=False` により **terminal failed (中止/欠落で恒久取得不能) を毎 tick 復活させない** = 無限リトライ + netkeiba 過負荷を回避 (block 失敗は `process_pending` が attempt を消費せず pending のまま 15 分間隔で retry)。`process_pending` は file-lock 済なので watch-auto と併走しても二重 fetch しない。blocking IO は `asyncio.to_thread` でオフロード。lifespan startup で起動・shutdown で停止。状態は `GET /api/results/auto`。`tests/test_shobu_auto.py::test_results_auto_enqueue_filters`。
- **市場一致シグナルの自動蓄積 (2026-06-30 ユーザ指示「市場一致一本に絞って蓄積→確証を取れるよう自動化」)**: 結果取得ループ (`_run_once`) が `process_pending` の後に `append_market_agreement_history` を呼び、**Claude#1 が市場1番人気と一致するか (consensus)** で券種 ROI を分割した現在値 (`compute_market_agreement`: 市場非依存レースで agree/disagree に分け 馬連/組合せ系/3連複BOX/本命系 の ROI と差Δの bootstrap CI を出す) を `data/cache/market_agreement_history.jsonl` に時系列追記する (`races` 不変なら dedup=no-op)。**Δの 95%CI が 0 を跨がなくなれば確証** (`significant`)。初期所見 (72R): 3連複BOX は Δ−72pt CI[−154,−7]=有意 (不一致=Claude contrarian 時に伸びる)、馬連は Δ+103pt だが CI[−28,+298]=要蓄積 (一致時に伸びる仮説)。**⚠ 2026-07-04 更新**: ①**3連複BOX の contrarian 有意性は 94R で失効** (Δ−72→現 −37pt CI[−106,+29]、agree 側に的中が出て縮小) — 「既に有意」扱いは取り下げ、全4系列とも蓄積中に戻った。馬連/combo も Δ 縮小傾向 (+77/+50pt, CI 0 跨ぎ)。②pooled ターゲット (combo/honmei) の bootstrap CI が**脚単位 iid 再標本化でレース内相関を無視し過小** (偽の★確証リスク) だったのを**レース単位合算**に修正 (`_pairs`、combo CI 実測 [−4,120]→[−28,164])。単一戦略ターゲットは 1脚/レースなので従来から正しい。③新たな蓄積中候補: **place1×市場一致 Δ+45pt CI[+5,+82] (105R・単一脚=レース単位CIで有意・v1/v2 とも同方向)** — trio1234box の失速が示す通りこの n では反転し得るので実装せず +50R 蓄積で再判定。`GET /api/shobu/market-agreement` (current+history)。**⚠ 2026-07-04 ダッシュボード表示を「買い方マトリクス」に変更 (ユーザ指示「①②③で分けるのでなくマトリクス表にして条件を組み合わせ状況毎の最良の買い方を見る」)**: いったん 市場一致(①)・拮抗/本命(②)・JRA/NAR(③) を別々の1次元スプリットとして足したが、ユーザ指示で **3 二値条件を掛け合わせたマトリクス**に統合。`compute_market_agreement` は各レースを **consensus (Claude#1==市場1番人気) × style (市場 top2 の implied 勝率比 `_FAVORITE_RATIO_THRESHOLD`=2.0 未満=拮抗型/以上=本命型・`_MARKET_INDEX_T`=1.5 で復元) × venue (JRA/NAR・banei は地方=NAR)** でタグ付けし、`matrix` として **2³=8 状況(行) × 4 券種(列=馬連/組合せ/3連複BOX/本命系)** の ROI + bootstrap CI を出す。各セルは `best_key` (その状況で ROI 最大=最良の買い方・**`_MATRIX_SAMPLE_FLOOR`=8R 未満のセルは推奨から除外**) と `confirmed` (ROI CI 下限>1.0=確定的に+EV★) を持つ。参考行に `overall` (条件なし全レース) と `market_baseline` (市場指数順の上位馬で同じ買い方=「ただ人気を買う」基準線、`_strategy_race_legs(ranking=)` で Claude 版と同一ロジック計算)。実測 (105R) の所見: **一致×本命型×NAR → 馬連 202% (最良)** / **不一致×本命型×NAR → 3連複BOX 133%** が目立つが CI 下限は未だ 100% 未満 (確証★なし)・JRA 系は各≤5R でサンプル不足。ダッシュボード `MarketAgreementCard` は状況(条件バッジ)×券種の1枚表で最良=緑・確証=★・サンプル不足=淡色表示。**consensus 1次元 `metrics` (agree/disagree ROI・Δ CI) は betGuide.ts が per-race 買い方ガイドで参照するため後方互換で残す**。history (`market_agreement_history.jsonl`) は `metrics` + compact `matrix` を蓄積。background: 買い方ガイド (`web/lib/betGuide.ts`) は当面 Claude 単独自信度 + consensus のみ (memory `feedback_strategy_confidence_rerun`)。`tests/test_shobu_pnl.py::test_market_agreement_splits_by_consensus` / `::test_market_agreement_matrix` / `::test_strategy_race_legs_ranking_override`。
- **プレレジ (事前登録) シグナルルール + walk-forward ガードレール (2026-07-05 ユーザ指示「研究中シグナルを用いて回収率を格段に上げる」)**: 研究中シグナルを行動に変える正攻法として **「発見と検証の分離」** を実装。walk-forward バックテスト (105R, look-ahead なし・レース t 以前の履歴だけでマトリクス best セルを選ぶ) で **セル追従は機能しない** ことを実測 (追従 ROI 48-66% < 馬連固定 78%・「確証セルのみ」は一度も発火せず・in-sample 主役の 馬連×一致×本命×NAR 202% は v2 時代 36% に崩壊 = trio1234box 失効と同型の bin-selection)。よってセル自動追従は実装せず、**ルールを定義凍結 (プレレジ) → 登録日以降のレースのみで ROI+bootstrap CI を蓄積 → 確証★ (n≥`SIGNAL_RULE_MIN_CONFIRM`=30 かつ CI下限>1.0) / 破綻 (n≥20 かつ CI上限<1.0) を自動判定** する台帳を作った。配線: `api/store.py` の `SIGNAL_RULES` (**定義変更禁止** — 変えるなら新 key で再プレレジ) + `_tagged_eval_races` (compute_market_agreement と共有の条件タグ付きレコードローダ・リファクタ出力同値確認済) + `compute_signal_rules` (insample=参考 / prospective=確証判定 / market_baseline=同条件で市場人気順に同じ買い方 / dead_cell / walkforward) + `append_signal_rules_history` (`data/cache/signal_rules_history.jsonl`, ResultAutoFetcher が毎 tick 追記・races 不変は no-op) + `GET /api/shobu/signal-rules`。UI: ダッシュボード `SignalRulesCard` (マトリクスカードの上・発見時/登録後/市場人気基準/状態 の表 + walk-forward ガードレール注記) と 予測詳細の「プレレジ」バナー (`betGuide.raceSignalRuleGuide` が per-race で発火ルール判定・死にセル=拮抗×不一致 は**見送りゾーン**表示・破綻ルールは非表示。条件定数 `MARKET_INDEX_T`/`FAVORITE_RATIO_THRESHOLD`/JRA 10場 は backend のミラー)。**登録ルール 6 本 (2026-07-05, 発見時 105R 実測)**: ①place2_bigfield (複勝2×≥12頭, 123%・drop-best 105% = 唯一 drop-best 後も 100% 超) ②place2_bigfield_agree (×一致, 162%/126%) ③place1_consensus (複勝1×一致, 97%。市場#1複勝全体 80%・Claude 不一致側の人気馬複勝 74% に対し**一致選別の付加価値が実在** — なお一致条件下は Claude#1=市場#1 で同じ馬を買うため market_baseline 列とは定義上同値になるのが正しい) ④win1_smallfield (単勝1×≤8頭, 123% だが drop-best 92%=単発依存疑い) ⑤quinella12_alive (馬連×死にセル回避, 97% vs 死にセル 47% — 見送り規律) ⑥quinella12_agree_honmei_nar (202% セルの追試・崩壊検証用)。固定戦略ベースラインは全て <100% (馬連 78% だが前半135→後半22 と不安定)。**確証★が出るまでは全ルール参考表示・実弾には接続しない** (確証後も接続は人の判断)。
  - **上位3頭ギャップ + 荒れ具合の特徴量ルール 4 本を追加プレレジ (2026-07-05 ユーザ指示「claude指数上位3頭の指数と市場との差・1,2,3の開き・4頭目との開き・市場のオッズの開き (荒れ具合) からどの券種が回収率が高くなるか研究中シグナルに入れて」)**: `api/store.py:_race_features(idx, mkt)` が発走前観測可能な数値特徴量を計算し `_tagged_eval_races` の各レコードに `features` として付与 — gap12/gap23/gap34 (Claude 指数 1-2位/2-3位/3-4位 の開き)・top3_rank_gap (Claude 上位3頭の Σ(市場順位−Claude順位)、正=市場が過小評価)・top3_idx_diff (上位3頭の mean(claude−market) 指数差)・fav_odds (市場1番人気の復元オッズ (100/market_index)^1.5、高い=荒れ模様)・top3_conc (市場 implied 勝率の上位3頭集中度、低い=混戦)。`_rule_matches` に汎用 `features: {name: {min/max}}` 条件を追加 (計算不能 None は不発火=保守的)。**発見は `scripts/signal_feature_sweep.py`** (読み取り専用・固定閾値グリッド × 全戦略の ROI/CI/drop-best/前後半/市場基準を一覧、108R in-sample)。**追加登録 4 本 (registered_at 2026-07-05, 発見時 108R)**: ⑦place2_top3undervalued (複勝2×top3_rank_gap≥5, 110%・drop 94%・前半113/後半106 と唯一安定・市場基準67%) ⑧wide13_top3consensus (ワイド1-3×top3_rank_gap≤0, 130%/drop114%・市場と上位勢の見立てが揃う時 Claude の並びが価値を持つ仮説) ⑨win1_rough_market (単勝1×fav_odds≥2.5, 119%/drop95% だが前後半 179/58 不安定・荒れ具合系代表) ⑩place3_toppack_tight (複勝3×gap23≤2, 159%/drop119% だが n=17 極小・「開き」系代表の参考登録)。スイープ下位は trifecta123 が全条件 0% (死に筋の再確認) のみで新たな見送り規律は無し。frontend: `SignalRule.features` 型 + `betGuide.raceSignalFeatures` (backend ミラー計算・同点タイブレーク同一) で per-race プレレジバナーにも発火。`tests/test_shobu_pnl.py::test_race_features_*` / `::test_rule_matches_features` / `::test_signal_rules_features_end_to_end`。
  - **券種比較グリッド 30 本を一括プレレジ + 新戦略 3 種 (2026-07-05 ユーザ指示「上から単勝1,単勝2,単勝3,複勝1,複勝2,複勝3,馬連1-2,馬連1-3,ワイド1-2,ワイド1-3 で並べる。条件の定義はそれぞれ固定感のあるものにして比較する」)**: `SIGNAL_RULES` に **10 券種 × 固定 3 条件 (無条件 / 市場一致 / 荒れ模様 fav_odds≥2.5)** の `grid_<戦略>_<条件>` ルールを自動生成で追加 (`_GRID_STRATEGIES`/`_GRID_CONDITIONS`)。条件を全券種で統一したことで「どの券種がどの状況で強いか」を同じ土俵で比較できる。このために **win2/win3 (指数2/3位の単勝)・quinella13 (指数1-3位の馬連) を STRATEGY_DEFS へ追加** (`_strategy_race_legs` に脚・STRATEGY_SHORT_LABELS にラベル。戦略くらべカード/的中バッジにも自動で載る)。STRATEGY_DEFS の表示順もユーザ指示の順 (単勝1,2,3→複勝1,2,3→馬連1-2,1-3→ワイド1-2,1-3→その他) に並べ替え。`compute_signal_rules` は rules をこの券種順 (グループ内は grid→個別発見ルール) にソートして返す (計 40 本, ~1.1s/計算)。**注意**: 新戦略の的中脚が increase する分、result final_odds に無い組の的中で no_odds になり得るが、実データは in-money 全券種保存 (2026-07-04〜) のため 108R で減少ゼロ (テスト fixture は要追記 — 同日修正済)。グリッド in-sample の初見: 一致×馬連1-2 120% / 荒れ×馬連1-3 135% / 荒れ×単勝1 119% が上位、荒れ×馬連1-2 48% / 荒れ×ワイド1-2 31% が下位 (すべて蓄積中・確証なし)。
  - **多視点敵対レビュー (2026-07-05, 4視点×反証2名) の確定修正 2 件**: ①**hindsight ガード** — 発走後に再生成された snapshot (claude_eval リトライ/過去レース手動 analyze) は web 検索が確定結果を拾い得るため、`scored_pre_start` (採点時刻<発走時刻, start_at 不明は False) を prospective 算入条件に追加 (実測 105R 中 7R が除外対象 = 実質的に効く)。insample/マトリクス等の表示専用計測は従来どおり (既知の許容)。②**consensus 同点タイブレーク** — 旧 `max(dict)` は index_compare 行順 (=Claude 指数降順) 依存で agree 側に倒れ market_ranked (馬番昇順) と矛盾 (実データ 20261002-2-8 で顕在・place1_consensus の対基準線 +6.9pt は全額この 1R の artifact だった)。`(-指数, 馬番)` の明示タイブレークに統一 (betGuide.raceSignalRuleGuide も同修正)。**棄却された所見** (実害経路なし・記録): ★判定は逐次監視+6ルール同時のため family-wise の一時的偽★確率が名目2.5%より上振れ (~10%/ルール規模) — ★は screening であり実弾接続前の人手再確認が前提 (CLAUDE.md 明記済) / compute_market_agreement+compute_signal_rules の per-request 再計算は 1000R 到達時 ~10s/render 規模 → キャッシュ化は宿題。死にセル表示は発火中ルールを隠さず併記に変更 (台帳母集団と per-race 表示の乖離防止)。`tests/test_shobu_pnl.py::test_signal_rules_*` / `::test_signal_rule_*` / `::test_walkforward_matrix_no_lookahead` / `::test_prospective_excludes_post_start_scored` / `::test_consensus_tiebreak_by_number`。
- **勝負レース(推奨)の締切5-7分前 自動再score (`api.main.ShobuPaddockRescorer`, make api 常駐, ユーザ指示 2026-06-30)**: 「勝負レース取得後も make api を回しておけばパドック情報を取れるように」。**make api 単独ではパドックが入らない**問題 (shobu の Claude 指数は scan 時刻=締切のずっと前に生成され、make api の常駐ループは結果取得 (ResultAutoFetcher) と shobu の market-only refresh (--no-llm) のみで指数を再生成しない) に対し、**make api 内に1分毎の常駐ループ**を追加。当日 `data/cache/shobu/<date>.json` の **recommended な NAR/JRA レース** を見て、**締切 `MIN_LEAD_SEC`(2分)〜`WINDOW_SEC`(7分) 前** に入ったものを score 段 subprocess (`scrape_keibago`/`scrape_jra --phase=score --snapshot`、env `KEIBA_SCORE_TIMEOUT=200`/`QUERIES_PER_HORSE=4`) で **Claude 指数をパドック込みで再生成** → snapshot 上書き → 乖離(基準B)/市場一致シグナルも自動反映。実弾投票はしない・watch-auto 非依存 (snapshot 再生成は idempotent)。**再score 失敗/timeout は無害**: `analyze._run_score_stage` が scores 空なら `.llm.json` を上書きしないので scan 時点指数は消えない (最悪「指数据え置き+オッズ更新」)。window 内で `_fired` により1レース1回のみ撃つ (当日 reset)。lifespan で起動/停止、状態 `GET /api/shobu/paddock-rescore`。`tests/test_shobu_auto.py::test_paddock_rescorer_due_window`。

- **ダッシュボードを 地方/中央/ばんえい に分離 (2026-07-05 ユーザ指示「JRAはいったん別ページに避難して。ダッシュボード（地方）とダッシュボード（中央）で分ける」→ 同日「ばんえいは地方と中央と同じように別ダッシュボードに分離」・backend/frontend の2エージェント並列実装)**: 計測系 4 エンドポイント (`/api/shobu/pnl`・`/api/shobu/indexed-pnl`・`/api/shobu/strategies-pnl`・`/api/shobu/indexed-strategies-pnl`) に **query param `venue=nar|jra|banei`** を追加 (`api/store.py:_venue_filter`: jra=race_type=="jra" / banei=race_type=="banei" / **nar=それ以外 (地方平地・race_type 欠落の旧 doc も nar に落とす)** / 省略=全件の後方互換・不正値 422・`version` と AND 併用可。ev.segment_of_rd と同じ3区分)。母集団 dict 段階で絞るので recommended_total/skipped_*/races_detail も venue スコープ。frontend は `web/components/DashboardView.tsx` (venue プロップ付き共有 server component) に本体を抽出し、**`/` = ダッシュボード（地方）(venue=nar)** / **`/jra` = 中央** / **`/banei` = ばんえい**、ナビは3リンク。**研究系カード (SignalRulesCard プレレジ台帳・MarketAgreementCard 買い方マトリクス) は venue を条件/軸として内包するグローバル研究台帳なのでフィルタせず地方ページのみに表示** (中央/ばんえいページは fetch 自体しない)。実測分割: 全110R = 地方101R + 中央9R + ばんえい0R。**研究中シグナル表の緑色表示 (同日ユーザ指示)**: 発見時/登録後セルの ROI≥100% は数値%を緑 (text-emerald-300)、登録後が緑のルールは条件の定義テキストも緑 (broken の rose が優先)。`tests/test_shobu_pnl.py::test_venue_filter_splits_nar_and_jra`。

**保存基準 (GCS/BigQuery 移行)** — 現状 raw HTML ~2.3GB / parquet <1MB なのでローカル維持で十分。**数百 GB を超える前に GCS bucket + BigQuery テーブルに移行**する想定 (新規スクリプトを書いて parquet を bq load → SQL で集計、raw HTML は GCS にミラー)。

**今日の勝負レース (`src/shobu.py` + Web UI `/shobu`, 2026-06-20 ユーザ指示)**: 当日の全レースを取得し「勝負レース (= 通常より賭ける価値が高そうなレース)」を抽出する Web ページ。ボタン 1 つで `discover_today_races` (netkeiba 非依存) → 各レースを **基準B (市場との順位乖離) 単独** で採点する (**2026-06-28 ユーザ指示「基準Aは消して基準Bをベースに・強弱いらない」で基準A=強弱/separation を完全削除**。`_separation`/`_implied_*`・use_separation/combine/sep_threshold/fetch_odds・強弱UI・sep_median を全レイヤーから撤去):
- **基準B: 市場との順位乖離** (2026-06-20 ユーザ指示で「単に Claude>市場」から変更) = 各馬を Claude 指数 / 市場指数でそれぞれ降順ランク付けし、`rank_gap = market_rank − claude_rank` (正 = Claude が市場より上位評価 = 市場過小評価) を見る。「市場2番人気なのに Claude 本命」= その馬の rank_gap=1。**乖離馬** = rank_gap≥1 かつ 指数差≥`edge_margin` (順位だけでなく数値の裏付けも要求)。**score (= shobu_score)** = `top_rank_gap·20 + Σ_edge(rank_gap·5 + max(0,指数差)·0.4)` (top_rank_gap = Claude本命の市場順位−1 が主軸)。`edge_threshold` 以上で勝負レース。指数は既存 snapshot の `index_compare` (無ければ `llm_win_index`/`market_win_index` の両方) から取り、両指数が揃う馬が2頭未満なら評価不能 (= 非推奨)。**既存スナップショット中心** (無料・即時)。ボタン押下で **全レースの Claude 指数を一括生成** (claude_all 既定ON, claude -p が Tavily/WebFetch で各馬を web 検索 → 指数 → snapshot)。OFF にすると `claude_eval N` で**発走が近い順** N 件のみ新規生成。
- **option (CLI / Web UI 両対応)**: しきい値 (edge_threshold / edge_margin)・対象 (all/jra/nar=地方平地/**banei=帯広ばんえい**, `ev.segment_of_rd` と同じ3区分で分離)・発走前のみ・Claude 一括生成 (claude_all) / 発走が近い順 N件 (claude_eval)・**取得レース数 (max_races)**。`shobu_score` = 基準B のスコアそのもの。UI は推奨を専用「勝負レース(推奨)」セクション (緑カード+番号) に分離し、推奨外は折りたたみ。
  - **取得レース数 (max_races) は「発走が近い (早い) 順」に N 件採用 (ユーザ指示 2026-06-30)**: `races[:N]` (start_at 昇順の先頭 = これから最も早く発走する=今すぐ賭けられる N 件)。これは `_select_claude_targets` (claude_eval) の「発走が近い順」と同方向。**旧挙動 (2026-06-28〜29 の `races[-N:]`=発走が遅い夜の N 件) は昼スキャン時に発走が近いレースを落とすため 2026-06-30 に反転**。`tests/test_shobu.py::test_scan_max_races_keeps_nearest_start`。
- **配線**: `POST /api/shobu/scan` (ShobuScanRequest) が `build_shobu_cmd` で Job を起動 (analyze と同じ subprocess+SSE log 機構) → 結果を `data/cache/shobu/<date>.json` に atomic 書き出し → `GET /api/shobu/result?date=` が配信。frontend は Job 完了を polling して result を再取得。各レース行は `/predictions/<race_id>` (内部 race_id join) へリンク。`tests/test_shobu.py` / `tests/test_shobu_auto.py`。
- **発走後も推奨レースを file に維持 = carry-forward (ユーザ指摘 2026-06-28)**: scan は file を上書きし `upcoming_only=True` が発走済を discovery から落とすため、**再スキャンするとそれまで推奨だった発走済レースが file から消え、ダッシュボード仮想収支 (`compute_shobu_pnl` は file の recommended を読む) が発走後に数えられなくなる**。対策として scan は **前回 file の発走済レース (当日母集団全体) を carry-forward してマージ・再採点**し、file を当日の累積記録にする (per-date file なので日跨ぎ累積はしない)。これで発走→結果確定後も勝負レースが仮想収支に反映され続ける。`tests/test_shobu.py::test_scan_carries_past_recommended`。
- **勝負レースの最新オッズ自動更新 (2026-06-21 ユーザ指示で方針転換)**: 勝負レースページを開いている間、**2分毎に推奨 (勝負レース) のみ最新オッズで再採点**する (`POST /api/shobu/refresh` → `shobu.refresh_recommended`)。各推奨レースの単勝を 1 fetch (NAR=keiba.go.jp / JRA=公式・netkeiba 不使用) し、市場との順位乖離 (基準B = `market_index` を最新オッズで `_market_index_from_odds` 再計算、Claude 指数は snapshot 据え置き) を recompute して勝負スコアを更新。**discovery も Claude -p も呼ばない**ので即時・規制リスク無し。勝負スコアの履歴を `data/cache/shobu/<date>.scores.json` に追記し、前回比 (`score_delta`) と時系列 (`score_history`) をレースに付ける → UI が ▲▼ デルタ + 極小スパークラインで表示。採点ロジックは scan と共通の `_evaluate_race` / `_build_summary` に切り出し済 (`claude_use_fresh_market=True` で基準Bも最新オッズ化)。フロントは `web/app/shobu/page.tsx` の 2分 interval (トグル「2分毎に最新オッズ更新」既定 ON・「今すぐ更新」ボタン併設)。**全レース再スキャン・Claude 再生成は従来どおり手動ボタン (`POST /api/shobu/scan`) のみ** (母集団更新や Claude 生成は重いので自動化しない)。`python -m src.shobu --refresh --date <YYYYMMDD>` で CLI 再採点も可。※ 旧方針 (2026-06-20「自動更新はしない」) はこの指示で撤回。
- **Claude 指数一括生成の並列度と keiba.go.jp レート制限 (2026-06-21 rc=1 調査)**: 一括生成は各レースを `python -m src.scrape_keibago/jra <rid> --phase=score` の **別 subprocess** で走らせ、`ShobuScanRequest.claude_eval_parallel` 個を ThreadPool で同時起動する。**keiba.go.jp / JRA公式は 1 IP からの同時アクセスをレート制限**し、~20 並列でスクレイプすると odds が**空**で返り → `analyze_keibago` が `KeibagoError("オッズが空")` → `scrape_keibago` main が `SystemExit(1)` → score subprocess が **rc=1** で死ぬ (= shobu ログの `[claude-eval] ... rc=1`)。結果 **Claude 指数がつかない**。実機確認: 21 並列スクレイプで全件 win 空 + 以後の sequential も一時ブロック。→ **across-race 並列 (`claude_eval_parallel`) は既定 4 に抑える** (keiba.go.jp を bursting しない)。深い検索の「20並列」は `KEIBA_LLM_MAX_CONCURRENT=20` (claude -p 同時数=keiba.go.jp 非依存) + `score_parallel` の per-race シャードが担う。`rc=1` を見たら claude_eval_parallel を下げる (or 時間を置いて再実行)。
- **「1件しか指数が付かない」= claude -p/Tavily 輻輳 + rc=0 誤判定 → 成功判定の修正 + 自動リトライ (2026-06-29 ユーザ報告の修正)**: 「最新5件を取得しても1件のみ Claude 指数が生成され他は推奨外」を実機 snapshot で解析した結論は **rc=1 (scrape 失敗) ではない**。失敗4レース (盛岡8-11R) は `odds_source=keibago`・market_index 充足で **scrape は成功**しており、`llm_fallback=True`・`.llm.json` 無し・research を ~7s で早期中断 = **claude -p / Tavily の同時実行輻輳** (across-race=4 × score_parallel シャード × `KEIBA_LLM_MAX_CONCURRENT`=20 で claude -p が大量同時 → Tavily/API 輻輳) で指数が出ず、しかも score subprocess は **rc=0 で正常終了** (analyze は phase=='score' で正常 return)。**旧 `_run_claude_eval` は rc=0 を成功にカウントし「5/5 生成成功」と誤報告**していた (rc!=0 ゲートのリトライは一切発火しない)。修正: ①成功判定を **rc ではなく「Claude 指数が実際に付いたか」** (snapshot の `index_compare[].claude_index` / 無ければ `.llm.json` の `scores`) で行う ②指数が付かなかったレースを **across-race 並列度を段階的に下げて (pass0=parallel → pass1=parallel//2 → pass2+=直列1) 最大 `max_retries`(既定2) 回リトライ** — 直列リトライは「最後に1レースだけ走って成功した」実機挙動 (12R) と同条件に収束するので確実に埋まる ③`claude_eval_parallel` の既定を全レイヤ **4 に統一** (旧: shobu CLI/`api/runner`=6, `api/main`=4)。`tests/test_shobu.py::test_run_claude_eval_rc0_without_index_is_failure` / `::test_run_claude_eval_retries_until_indexed`。
  - **⚠ 2026-07-04 追加修正: リトライで env も軽くして自己回復 (`_pass_env`)**: 上記②は across-race 並列度だけ下げていたが、各 score subprocess は **10クエリ/頭 × ARCH-A シャード並列のまま重い**ので、pass1 以降も同じ輻輳で timeout し続ける実機を確認 (夜NARスキャンが `--score-parallel --score-queries-per-horse 10 --claude-eval-parallel 4 --llm-max-concurrent 20` で **24分・指数0**、しかも **load 1.3/24 = CPU でなく Tavily/Anthropic API の同時アクセス過多**によるスロットリングで 1 セッションが遅く ~14s/ツール呼び出し、リサーチ+採点が内側 timeout(810s) 内に届かず全 `fallback`)。→ `_run_claude_eval._pass_env(attempt)` を追加し **リトライ (attempt≥1) では env を単一セッション化** (`KEIBA_SCORE_PARALLEL=""`・シャード env 除去)・**低クエリ** (`KEIBA_SCORE_QUERIES_PER_HORSE`=min(operator, pass1→4/pass2+→3))・**低同時数** (`KEIBA_LLM_MAX_CONCURRENT`=min(operator,4))・**内側 timeout クランプ** (外側の手前) に落として輻輳を解く。across-race 並列 (4→2→1) と併せ、pass1 は 最大 2レース×単一セッション=同時 claude -p 2、pass2 は 1レース×単一=1 まで下がり、既知の成功条件 (単一セッション低クエリ) に確実に収束する。pass0 は operator 設定を尊重 (重い設定でも自己回復するので UI 側で軽くする必要は薄れた)。`tests/test_shobu.py::test_run_claude_eval_retry_lightens_env`。**注意**: 実行中スキャンは起動時の src/shobu.py を import 済なので、この修正は**次のスキャンから**効く (走行中スキャンは救えない)。
- **検索クエリ数とシャード飽和 (2026-06-28 ユーザ指示)**: shobu の Claude 指数は **1頭につき10クエリ = 頭数×10 クエリ** を流す (`ShobuScanRequest.score_queries_per_horse=10`・CLI `--score-queries-per-horse 既定10`)。`_run_claude_eval` が score subprocess に env を渡す: ①`KEIBA_SCORE_QUERIES_PER_HORSE=10` (並列パスは `_shard_numbers` が全馬を被覆するので合計=頭数×10、単一セッション=`score_horses_stream` も同 env を読むので **<8頭の小頭数でも頭数×10**) ②**並列飽和**: across-race=`claude_eval_parallel` に対し シャード/レース = `KEIBA_LLM_MAX_CONCURRENT // claude_eval_parallel` (既定 20//4=5)・`KEIBA_SCORE_HORSES_PER_SHARD=3` で各レースを細かく刻み、claude -p 同時実行を上限近くまで埋めて research を速める (per_shard を小さく=各シャードの担当頭数減=速い)。シャードを増やしても scrape は across-race=4 のままなので keiba.go.jp は bursting しない (claude -p は Tavily=別系統) ③`KEIBA_SCORE_TIMEOUT = min(max(120, claude_eval_timeout − 90), claude_eval_timeout − 10)` で内側 claude を外側 subprocess kill の手前に**必ず**収める (外側が先に発火すると score 結果が丸ごと失われ rc≠0 で指数なしになるため。timeout が小さくても反転しないよう二重に上限クランプ)。CLI `--claude-eval-timeout` は **下限 210s にクランプ** (内側 research 60s + scoring 60s floor + scrape が外側 kill より先に終わるのを保証)。operator が env で per_shard/max_shards/timeout を明示していればそれを尊重。`tests/test_shobu.py::test_run_claude_eval_*` / `tests/test_score_parallel.py::test_single_session_respects_query_env`。
- **補強根拠 (evidence) の上限撤廃 + 詳細表示 + クエリのログ出力 (2026-06-28 ユーザ指示)**: 「補強根拠を3つまでではなく10個以上あってもよい・あればあるだけよい・もっと詳しく表示する・クエリもログに出す」。①score プロンプト3種 (`build_horse_score_prompt`/`build_horse_research_prompt`/`build_horse_score_from_research_prompt`) の `support`「0-3+」を**上限なし**に変更し、各馬の補強根拠を **`evidence` 配列に 1 件ずつ具体的に全件 (10件以上可・多いほどよい)** 書かせる。`support` は `len(evidence)` (= `parse_horse_scores`/`_merge_research` が evidence 件数で backfill)。`llm._normalize_evidence` (件数上限 40・1要素300字クランプ、3で打ち切らない) で正規化。②`<race_id>.llm.json` に `evidence` を保存 (`_save_llm_scores`)、`_load_llm_scores` は **6-tuple (…, alerts, evidence)** を返す (全 unpack site = analyze×3 + scrape_keibago/jra/oddspark を更新)。`_build_index_compare`/`_save_prediction_snapshot` が `llm_evidence` を snapshot の `index_compare[].evidence` に通す (root=support は evidence 件数に必ず一致させる)。予測詳細ページ (`web/.../predictions/[raceId]/page.tsx` IndexCompareCard) が各馬行の下に「補強根拠 N件」を `<details open>` で全件展開表示。**この上限撤廃を境に Claude 指数の方針バージョンを v1/v2 として区別する (ユーザ指示 2026-06-30, 下記)**。③検索クエリをログに出力: `analyze._run_score_stage` の tool_use ログを **全文表示** (旧 70字 truncate を撤廃・`style="dim"`+`markup=False`+`soft_wrap=True` で角括弧 MarkupError と 80桁折返しを回避) し、shobu `_run_claude_eval._one` は score subprocess を **`subprocess.Popen` 化**して `🔍` クエリ行を `[query] <場><R>R 🔍 …` で scan ログへ転送 (`PYTHONUNBUFFERED=1` で即 flush、`threading.Timer` で旧 `subprocess.run(timeout=)` と同じ kill セマンティクス、`_safe_log` Lock で worker/main の log を直列化)。`tests/test_score_parallel.py::test_*_evidence*` / `tests/test_shobu.py::test_run_claude_eval_forwards_query_lines`。
- **Claude 指数 方針バージョン v1/v2 の表示 (2026-06-30 ユーザ指示)**: 「補強根拠が3件だったのを v1、無制限の現行を v2 として表示。左上タイトルの横にも」。**v1 = evidence 3件上限 (〜2026-06-27) / v2 = 無制限 (2026-06-28 commit 78a248c〜) / v3 = 仮指数アンカー±調整 (2026-07-01 15:13 commit dbff5b2〜・現行、2026-07-04 追加)**。定数 `src/llm.py:INDEX_VERSION="v3"` / `INDEX_V2_SINCE="2026-06-28"` / `INDEX_V3_SINCE="2026-07-01T15:13:17"`。v3 は「根拠の無い馬は仮指数据え置き」で生成分布が v2 と別物なのに v2 系列に混在していたため分離 (多角レビュー 2026-07-04)。**07-01 15:13〜定数更新までの snapshot は "v2" が誤刻印されており、`index_version_of` が採点日時で v3 に矯正する**。snapshot 保存時に `_save_prediction_snapshot` が Claude 指数があるとき `index_version` を刻む (`analyze.py`)。**旧 snapshot は欠落 → `api/store.py:index_version_of` が採点日時 (llm_scored_at→saved_at) で推定** (INDEX_V3_SINCE 以降=v3 / INDEX_V2_SINCE 以降=v2 / 以前=v1 / 指数なし=null)。**「指数なし」判定は index_compare の行存在でなく claude_index の実在** (market-only refresh の snapshot が v1/v2 に誤分類され version 母数が 53 件過大だったのを 2026-07-04 修正)。`list_predictions`/`get_prediction` が `index_version` を返す (実測: 全 609 件で v2=63 / v1=539 / null=7)。フロント: ①**左上タイトル横に現行版バッジ** (`web/app/layout.tsx`、`web/lib/version.ts:INDEX_VERSION`) ②予測詳細ヘッダと履歴一覧の各行に per-prediction の `指数 v1/v2` バッジ (`predictions/[raceId]/page.tsx`・`PredictionsList.tsx`)。`tests/test_api_store.py::test_index_version_*`。
  - **計測をバージョン毎に分離 + β(市場由来)を3分割表示 (2026-06-30 ユーザ指示)**: 「v2が上・v1が下」→さらに「市場由来の頃のはβ版として残して表示」。**バージョンは β/v1/v2 の3区分**: `index_version_of` が採点時刻で判定し、**市場由来 cutoff (`MARKET_INDEPENDENT_CUTOFF_ISO_JST="2026-06-21T19:04:27"`, commit 022b003 で score プロンプトから単勝オッズ列を撤去=Claude 指数が市場非依存になった時刻) 以前は β** (score に市場オッズがあった頃)・〜06-28未満は v1・06-28以降は v2。BOX 収支 (`_shobu_box_pnl`/`compute_indexed_pnl`) と 戦略くらべ (`_strategies_pnl`/`compute_indexed_strategies_pnl`) に `version` 引数を追加しループ内で `index_version_of(snap)!=version` を除外 (version 指定時は `recommended_total` もそのバージョン数)。**当初は市場由来を計測除外する方針だったが、ユーザ指示で β として残して表示に変更** (除外フィルタは撤去)。API は `?version=β|v1|v2` (β は encodeURIComponent)。ダッシュボード (`web/app/page.tsx`) は **v3→v2→v1 の順** (2026-07-04〜、β は対象少で非表示) に縦並び (`VersionMeasurementSection`/共有 `components/VersionHeading`、v2=accent / v1=muted / β=amber dashed・対象0は畳む)。実測: β=2R / v1=46R / v2=26R で重複なく合算一致。なお **v1>v2 (ROI 109% vs 32%) は市場由来のせいではなく小標本分散** (β は2Rのみ・v2は26R/4的中で 3連単BOX ROI は1発で大きく振れる)。`tests/test_shobu_pnl.py::test_strategies_version_split` / `tests/test_api_store.py::test_index_version_*`。
  - **競馬場別の内訳ページ (2026-06-30 ユーザ指示「競馬場毎にカードで内訳」)**: `compute_venue_breakdown(version)` が全体計測の per-race detail を **venue で group 集計** (BOX + 各戦略を `_roi_block` 形に)。`GET /api/shobu/venue-breakdown?version=`、`api.venueBreakdown()`。**新ページ `/venues`** (`web/app/venues/page.tsx`、nav「競馬場別」) が版毎 (v2/v1/β) に **競馬場カードのグリッド** を表示 (各カード=競馬場名+R数+BOX収支大表示+戦略別の小テーブル)。的中率の母数はレース数 (本体と同規約)。
- **長期 +EV を保証する指標ではなく「賭ける価値が高そうなレース」の screen**。
- **ダッシュボードの主役を「勝負レース仮想収支」に変更 (2026-06-21 ユーザ指示)**: 実弾投票束 (EV束/3連単束) の Live P/L 表示はダッシュボード (ホーム/確率較正) から撤去し、代わりに **Claude 指数上位 N 頭の3連単 BOX を仮定した「勝負レース仮想収支」** (`GET /api/shobu/pnl`, `api/store.py:compute_shobu_pnl`/`_shobu_box_size`、例 7頭立て=上位4頭 BOX) を主役表示にする。各レースの Claude 指数上位馬で BOX を組み、確定結果と照合した仮想 P/L を集計する (実弾の露出ではなく screen 指標の検証用)。
- **shobu 評価レース全体の仮想収支を別カードで併記 (2026-06-28 ユーザ指示)**: 「Claude 指数が全ての馬についていて結果があればダッシュボードに反映する」。勝負レース(推奨)収支とは**別カード (非破壊)** で、**推奨に限らず shobu が評価した全レース** (= 当日スキャンの母集団) の上位N頭3連単BOX 仮想収支を表示 (`GET /api/shobu/indexed-pnl`, `api/store.py:compute_indexed_pnl`)。**当初は `data/predictions` 全体を母集団にして 153 件に膨れた (betting pipeline の過去スコア ~06-12〜 が混入) が、ユーザ指摘「全レースがこんなに多いはずがない・ほとんど推奨のはず」で `data/cache/shobu/*.json` が評価したレース (recommended + 非recommended) に scope 修正** → 推奨カードの proper superset (実測 推奨47 ⊆ 全48・ほぼ推奨)。指数条件は推奨カードと同じ (BOX 可能=指数3頭以上+結果確定)。BOX/的中/配当は共通コア `_shobu_box_pnl(..., recommended_only=)` + `_box_race_pnl` に集約 (compute_shobu_pnl=recommended_only=True / compute_indexed_pnl=False、重複なし)。`api.indexedPnl()`。`tests/test_shobu_pnl.py`。
  - **ダッシュボードの主役を「shobu 評価レース全体」に変更・推奨 hero を撤去 (2026-06-30 ユーザ指示)**: 「Live P/L — 勝負レース (上位N頭3連単BOX) は不要 (shobu 評価レース全体を見るため)」。recommended のみの BOX hero (旧 2026-06-21 の主役) は撤去し、**`IndexedPnlCard` (shobu 評価レース全体・推奨は superset) を主役**に昇格。per-race 明細も `indexed.races_detail` に切替。`compute_shobu_pnl` (recommended BOX) 自体は API/`calibrate` ページ用に残置 (ダッシュボードからのみ撤去)。`web/app/page.tsx`。
- **Claude 指数 単純戦略くらべの仮想収支 (2026-06-30 ユーザ指示)**: 「Claude指数1位の単勝 / 2位の複勝 / 3位の複勝 / 指数1-2の馬連 / 単複 を仮定した計測を過去分全て表示」。BOX とは別の戦略比較カードとして、各 shobu 評価レースで以下の戦略を各脚 ¥point_cost で買ったと仮定し、`data/results/<id>.json` の `final_odds` (`win:N`/`place:N`/`quinella:a-b` = ×100 オッズ、例 `win:7=19.2`→¥1920) で **戦略ごとに** 収支集計 (`api/store.py:_strategy_race_legs`/`_strategies_pnl`、`STRATEGY_DEFS` が表示順)。戦略 (ユーザ指示 2026-06-30 で順次追加): **win1**(指数1位単勝) / **place1**(1位複勝) / **place2**(2位複勝) / **place3**(3位複勝・複勝は1,2,3位を分離) / **quinella12**(1-2位馬連・上位2着で判定・key 昇順) / **wide12**(指数1-2位ワイド・両馬とも上位3着で的中) / **wide13**(指数1-3位ワイド・判定は wide12 と同型、ユーザ指示 2026-07-02。初期実測 104R: 的中22% ROI 64% CI[39,91] — wide12 と実質差なし) / **exacta12**(指数1→2位の馬単・1着=top1∧2着=top2 の着順一致) / **trifecta123**(指数1→2→3 の3連単・着順完全一致) / **trio123**(指数1-2-3 の3連複・順不同) / **trio1234box**(指数1-2-3-4 の3連複BOX=C(4,3)=4点・上位3着が top4 に収まれば的中) / **wide123box**(指数1-2-3 のワイドBOX=C(3,2)=3点・各ペア両馬上位3着で的中・複数同時的中あり)。odds key は final_odds 規約に合わせ exacta/trifecta=着順そのまま・quinella/wide/trio=昇順。**※ 単複 (winplace=1位単勝+1位複勝) は一度追加したが 2026-06-30 ユーザ指示で全表示から撤去** (`STRATEGY_DEFS`/`_strategy_race_legs`/`_WINPLACE_MIN_SYNTH` 削除)。
  - **買い見送りフィルタ + 的中率の母数 (ユーザ指示 2026-06-30)**: ①**全券種で最終オッズ ≤1.1 なら買わない** (旨味の無い大本命除外, `_MIN_ODDS`)。当初 単勝/複勝のみ → ユーザ指示「全ての馬券は ≤1.1 なら買わない」で全券種へ拡張。フィルタ判定のオッズは **snapshot の `bet_tables`** (組番別, `_snap_combo_odds`。順不同券種は昇順正規化して照合)。**⚠ 実態 (2026-07-04 多角レビュー実測)**: ①shobu 経路の snapshot は全て stage="score" なので判定オッズは**スキャン時/自動再score 時 (推奨は締切2-7分前, 06-30〜) のもの**で「締切直前の最終オッズ」ではない (06-30 以前のスキャン分は最大9時間前のオッズ・fired 脚の realized>1.1 が 41%)。②複勝/ワイドは全 writer がレンジ**下限**のみ保存するためフィルタは下限判定=保守的に過剰発動 (place1 の母集団を ~44% 削る・fresh でも fired 複勝の 31% が実払戻 1.2-1.9)。**→ いずれも仕様として据え置き** (2026-07-04 判断): 閾値 sweep (1.0〜2.0) は戦略別最適が非単調 = bin-selection の罠でどの閾値でも ROI<100%、途中で定義を変えると系列の母集団定義が混在する。stale 問題は自動再score の配備 (06-30) 以降の新規データでは実質解消。win/place は全馬充足 (実測 109/109・107/109)、組合せ券種は経路により疎 (netkeiba 経路は pair 系が空・keibago/jra 経路は揃う) → 表に組番が無ければ買う (オッズ不明)。result の final_odds は in-money 組のみ=着順依存で不適なので使わない。払戻は従来どおり result final_odds。見送った脚は stake/payout/bets/races から除外。②**的中率の母数は脚数でなくレース数** (`races_hit/races`)。特に trio1234box (4脚/レース) や wide123box (3脚/レース) は脚母数だと過小 (例 trio1234box 旧 11/292=3.8%→新 11/74=14.9%)。`strategies[].races_hit` を追加。(③ 旧「単複は合成オッズ<1 で見送り」は winplace 撤去で廃止)。**複勝は頭数ルール** (`_place_cutoff`: 4頭以下=発売なし / 5-7頭=2着まで / 8頭以上=3着まで)。母集団は BOX と同じ `_shobu_eval_races(recommended_only=)` を共有。払戻オッズは **的中脚のみ要求** (外れ脚は¥0)、的中脚のオッズが無い (final_odds 未保存) レースのみ `no_odds` で分母外 (final_odds は all-or-nothing なので exotic 追加で no_odds は実質増えない)。返り値 `strategies` は各戦略の bets/hits/hit_rate(+CI)/stake/payout/net/roi(+CI) を分離 (trio1234box は 4脚/レース・stake ¥400/レース)。`GET /api/shobu/strategies-pnl` / `/api/shobu/indexed-strategies-pnl`、`api.strategiesPnl()`/`indexedStrategiesPnl()`、ダッシュボードは `StrategiesPnlCard` (比較テーブル) で表示。**初回実測 (2026-06-30, 全評価 72R)**: 単勝#1 ROI85% / 複勝#1 74%(的中58%) / 複勝#2 90%(的中49%) / 複勝#3 75%(的中36%) / **馬連#1-2 101%(的中17%)** / 馬単#1→2 98%(的中8%) / 3連単1→2→3 0%(的中0=順序一致は稀) / 3連複1-2-3 13%(的中3%) / 3連複BOX1-2-3-4 56%(的中4%/脚) / 単複 83%。いずれも n 小で CI 広い (どれも控除を覆す確証なし)。`tests/test_shobu_pnl.py::test_strategies_*`。
  - **カードに的中券種ラベルを表示 (2026-07-04 ユーザ指示「勝負レースのカードと予測分析履歴のカードに的中した券種ラベル (ダッシュボードで表示している仮の購入) を表示。EV束の的中は関係ない」)**: `api/store.py:hit_bet_labels(snap, result)` が **ダッシュボード仮想購入 = 上位N頭3連単BOX (`_box_race_pnl`) + 戦略くらべ (`_strategy_race_legs`)** の的中を `[{key,label,payout}]` (payout=¥100/脚換算・BOX は的中組合計) で返す (判定・≤1.1 フィルタ・同着 rank は計測本体と同じ共通ヘルパ → races_detail と必ず一致。指数<3頭/結果未確定/no_odds は null)。露出: ①`list_predictions` の各行に `hit_strategies` ②shobu result は配信時 enrich (`attach_hit_labels`: `get_shobu_result` と `POST /api/shobu/refresh` 応答。scan file 自体には書かない)。フロントは共有 `web/components/HitBetBadges.tsx` (「仮想的中」+ 緑バッジ列・ラベルは `STRATEGY_SHORT_LABELS`) を `PredictionsList` の行 (showHits 連動) と shobu `RaceCard` (reasons の上) に表示。EV束/3連単束 (実弾) の的中バッジとは独立。`tests/test_shobu_pnl.py::test_hit_bet_labels_*`/`test_attach_hit_labels_*`/`test_list_predictions_carries_hit_strategies`。
  - **2026-07-04 多角レビュー (5次元ファインダー→3視点敵対検証) の確定バグ修正**: ①index_version_of の market-only 誤分類 ②v3 未刻印 ③同点タイブレーク行順依存 ④_shobu_eval_races の dedup 順序 (latent) ⑤pooled CI レース内相関 ⑥keibago/JRA final_odds レンジ下限 ⑦同着取りこぼし — 詳細は各節に追記済。**棄却された所見** (敵対検証で不成立): no_odds 非対称の実害 (現母集団は netkeiba 源のみで未発火)・bet_tables top-30 truncation (フィルタ no-op 方向で安全)。**条件付き実測 (104R) の要点**: place2×≥12頭 ROI 127% (n=30, drop-best 108%) と win1 の頭数単調性 (≤8頭 123%*→9-11頭 88%→≥12頭 41%) が蓄積ウォッチ対象、trifecta123 は 0/104 で死に筋。wide13 が wide12 に優位と言える条件は現データに無い (v1/v2 間で優劣反転=ノイズ)。version 分割 (v1/v2/v3) は期間非重複の時代タグでありレバーではない。
- **頭数別の最適 BOX N の探索 (2026-06-30 ユーザ指示, `scripts/optimal_box_n.py`)**: shobu 評価レースを出走頭数でバケットし上位N頭3連単BOX (N=3..8) を sweep して ROI/的中率/bootstrap CI を出す読み取り専用バックテスト。**結論 (実測 69R)**: 現行 `_shobu_box_size` (8頭以上=5 / 7頭=4 / 少頭数は最低3頭場外) が in-sample ROI ~78% で**全固定Nルール中ベスト** (固定N=4=74% / N=5=76% / N=3=14%[的中2.9%で過少] / N=8=49%[買い過ぎ])。頭数別の「最適N」(8-9頭→5, 10-11頭→4 等) は各バケット数十Rで CI が大きく重なり overfit 領域 (CLAUDE.md の bin-selection 破綻の教訓と同型) → **現行ルール据え置きが妥当**。レース蓄積後に再 sweep して確認する。



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

- 控除率は券種で違う (`src/ev.py PAYOUT_RATE`): **3 連単は JRA/NAR とも 27.5% (払戻率 72.5%)**、単複 20%、馬連/ワイドは JRA 22.5% / **NAR 25%**、馬単 25%、3連複 JRA 25% / NAR 27.5%。市場効率では `P × O ≒ 払戻率` (3連単なら ≒0.725)。※旧記述「3連単 22.5%」は誤り (2026-06-10 修正)。数学的に最もハードルが低いのは単複 (80%)。
- **複勝の出走頭数ルール (2026-06-10 修正)**: JRA/NAR とも出走 **7 頭以下は複勝の払戻が 2 着まで**、**4 頭以下は発売なし**。`ev.place_prob` / `portfolio._bet_hits` / `api/store` / `bundle_calibration_report` が頭数ルールを適用済 (以前は常に top-3 扱いで、少頭数 NAR の複勝 EV が過大 + 計測に幻の的中が乗っていた)。
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
5. **Claude 考察 → 各馬指数 (2段パイプライン, 2026-05-31〜)**: LLM (`claude -p`) の役割を「最終買い目の picks/cuts 選定」から **「各馬の強さ指数 (0-100, 高い=1位) を出す」** へ変更し、その指数を確率モデルの fundamental に合成する。
   - **claude -p usage limit → Anthropic API 自動フォールバック (2026-07-04 ユーザ指示、同日中に gate 修正)**: claude -p は subscription 認証 (`_claude_env` が ANTHROPIC_API_KEY を除外) なので Pro/Max の usage limit に当たると結果を出せない。`score_horses_stream` / `select_trifecta_stream` は `.env` の `ANTHROPIC_API_KEY` で **Anthropic API を直叩き** (`llm._api_stream`, SDK `anthropic`) してフォールバックする。score 段は server-side `web_search_20260209` (回数上限 env `KEIBA_API_FALLBACK_MAX_SEARCHES`=32) 付き・3連単選定は検索なし。モデル既定 `claude-opus-4-8` (env `KEIBA_API_FALLBACK_MODEL`)、adaptive thinking + effort=high、pause_turn 継続 ≤6。イベント形式は claude -p と同一 (`tool_use`/`text`/`result`) なので下流 parse/ログ転送はそのまま効く。**通常時は API 課金なし**・`KEIBA_API_FALLBACK=0` で無効化。並列 score (ARCH-A) が limit で全滅した場合も dispatcher が単一セッション → API の順で落ちるので被覆される。
     - **⚠ 発火条件を「テキスト一致」から「timeout 以外で result 未取得」に修正 (2026-07-04 同日中)**: 初版は `_looks_rate_limited` (`"usage limit"`/`"rate limit"` 等の文字列一致) が true のときだけ発火する gate だったが、実機で claude -p が usage limit 到達時に **assistant/result/error イベントを一切出さないまま rc!=0 で無言終了**する挙動を確認 (2026-07-04 朝、福島/小倉/函館 JRA レースが `llm_fallback=True`・`tool_usage` ログ0件のまま生成失敗、`git show HEAD` 直後の実機テストで claude CLI バイナリを逆コンパイルし `rate_limit_event` (`status`/`overageStatus`/`isUsingOverage`) というストリームイベント形式を確認したが、hard-block 時の実際のテキストは不明で `_looks_rate_limited` が一致しなかった)。この無言失敗はテキストマーカーに一切引っかからず旧 gate が発火しなかった実障害。→ **gate を「`timeout` 以外で `saw_result` が False なら無条件フォールバック」に変更** (`limit_hit`/`_looks_rate_limited` はログの理由表示にのみ残す。timeout だけは従来どおり除外 — 単に遅いだけの可能性がありフォールバックしても遅さは解決しないため)。API 呼び出し自体は claude -p が失敗したときにしか走らないので通常運用の課金は増えない。`tests/test_llm_api_fallback.py` (`test_score_falls_back_on_generic_failure` / `test_score_no_fallback_on_timeout`)。
   - **score ステージ** (締切5-7分前): `llm.score_horses` (dispatcher) が Tavily/WebFetch で各馬の適性・状態・取消を調べ `data/predictions/<race_id>.llm.json` に指数キャッシュ。`analyze._run_score_stage` が4経路 (netkeiba/JRA/keibago/oddspark) 共通で呼ぶ。
     - **前走戦績は公式データを prompt に提示し検索しない (2026-06-29 ユーザ指示)**: 「前走戦績は NAR公式/JRA公式から確実に取って見る・それ以外を調べて評価」。各馬の `past_runs` は score 時点で既に公式自給済 (keibago=`HorseMarkInfo`=地方競馬公式 / JRA=`accessU`=JRA公式 / netkeiba 馬柱 / oddspark)・leakage 防止 (対象日以降除外・直近5走) 済み。これを `build_horse_score_prompt` が **`## 前走戦績 (公式データ・検索不要)` セクション** (`_render_past_runs_lines`/`_fmt_past_run`、1走=`MM/DD 場R クラス 馬場距離going 頭数 人気→着 上りX 通過Y`) として出走馬表の直後に描画 → Claude が **着順/距離/馬場/頭数/人気/上り/通過を web 検索せず読め**、検索予算を「それ以外」(①直前情報②軟情報③騎手成績) に集中できる。検索ルールも has_past 時に「前走戦績は上の公式データ参照・再検索しない」へ自動 reword。`past_source` ラベルを `_run_score_stage`→`score_horses`→各 prompt builder へ thread (4経路が自経路のソース名を渡す)。並列 ARCH-A でも 前走戦績は partition の `pre` (検索 MCP マーカー前) に入るので RESEARCH/SCORING 子に伝播 + `build_horse_research_prompt` 側 rules も reword。past_runs が1頭も無ければセクション省略=従来どおり近走を検索 (degrade)。**section header は partition マーカー (`## 検索 MCP の運用ルール`/`## 指数の付け方`) と非衝突**で string-surgery 安全。`tests/test_score_parallel.py::test_past_runs_*`。
     - **パドック評価を締切5分前に取り込む強化 (2026-06-30 ユーザ指示)**: 「パドック評価も反映した上で指数・市場乖離を計算したい。締切5分前に自動で取れるか」→ **既に score 帯=締切5分前 (`--score-window 5`) が claude -p+Tavily で直前情報(馬体重)・軟情報(パドック気配)を検索し指数に反映、shobu 基準B がその指数 vs 市場の乖離を計算済み**だったので、新スクレイパは作らず既存検索を強化した (ユーザ選択「既存の5分前検索を強化」)。score プロンプト 2 種 (`build_horse_score_prompt` 単一 / `build_horse_research_prompt` 並列) に、①共有の①直前情報セクションでパドックを「締切~5分前に実施中の最重要直前情報 (落ち着き/イレ込み/発汗/歩様/毛艶/トモ/気配上下)・各馬専用クエリで必ず確認」へ格上げ、②検索ルールに「**各馬まず『パドック/当日馬体重/気配』専用クエリを1本投げる**」を必須化、③クエリ例に `"<馬名>" パドック OR 気配 OR 歩様 OR 馬体重 <YYYYMMDD>` を追加。新ソース不要・netkeiba block 非依存 (Tavily 汎用web)・タイミングは既存 score 帯にそのまま乗る。乖離(基準B)・市場一致シグナルは指数更新を自動で反映。`tests/test_score_parallel.py`。
     - **score 段ツール利用の永続化 (2026-06-30 ユーザ指示)**: 「Tavily が WebSearch/WebFetch より優れているか過去履歴で分かるか」→ **分からなかった** (①score 段の `ALLOWED_TOOLS` は Tavily(search/extract)+WebFetch+Read で **WebSearch は allowlist 外=未使用** ②tool_use ログは Job の in-memory deque(maxlen=4000)のみで未永続 ③`.llm.json`/snapshot は evidence を保存するが「どのツール由来か」を記録せず outcome と紐づかない)。→ 今後比較できるよう **`analyze._run_score_stage` の tool_use 分岐で `_append_tool_usage(race_id, name, query)` が `data/cache/tool_usage/<race_id>.jsonl` に永続化** (`_classify_tool`: search/extract/fetch/websearch/read/other)。4経路共通の `_run_score_stage` を通るので 単一/並列・watch-auto・自動再score 全てで蓄積。`scripts/tool_usage_report.py` で種別/ツール別の使用頻度を集計 (質=的中寄与の比較は outcome 紐付け/A/B が要るので蓄積後)。失敗は呑む (score を止めない)。`tests/test_score_parallel.py::test_classify_and_persist_tool_usage`。
     - **Claude 指数は市場非依存 (2026-06-21 ユーザ指示)**: score プロンプト (`build_horse_score_prompt` / 並列 `build_horse_research_prompt` / `build_horse_score_from_research_prompt`) の出走馬表から **単勝オッズ列を撤去**し、「①②で根拠が無い馬はオッズの常識水準に留める」等の**オッズ・人気アンカーを除去**した → 各馬指数は **市場を一切見ない検索ドリブン**になる。**適性総合 (`aptitude.py`) は過去走/feature 由来でオッズ非依存**なので表に残置 (検索の手がかり)。市場は後段 `estimate_probs` の market ブレンドでのみ反映 (二重カウント回避)。これで shobu の市場乖離 (基準B) や bet の Claude 主導P (~75%) が真に市場と独立した signal になる。プロンプトの「edge=市場に直交する情報」哲学 (公開情報は既にオッズに織込済=無価値) の文言は概念説明として残す (オッズ値は渡さない)。`tests/test_score_parallel.py`。
     - **検索並列化 (`KEIBA_SCORE_PARALLEL`, 既定 OFF, 2026-06-18)**: 既定は従来の単一 `claude -p` (`score_horses_stream`, **頭数 × `KEIBA_SCORE_QUERIES_PER_HORSE` クエリ・env 未設定なら 2/頭**。2026-06-28: 単一セッションも env を読むので shobu の 10/頭 が小頭数レースにも効く)。`KEIBA_SCORE_PARALLEL=1` で **ARCH-A プロセス並列** に切替: 検索の重い部分を K 個の `claude -p` RESEARCH 子プロセスに分割 (各シャードは担当馬を高検索予算で調べ **事実(facts)だけ返す = 0-100 は付けない**) → 1 個の `claude -p` SCORING 段が全馬+収集事実を見て **レース内相対 0-100 を一括採点**。相対性は採点が単一段に閉じることで構造的に保たれる (相対性を壊さずに検索を大幅増やせる)。どこかで result を出せなければ単一セッションにフォールバック (= 既定挙動)。env: `KEIBA_SCORE_QUERIES_PER_HORSE` (既定 6, 旧 2) / `KEIBA_SCORE_HORSES_PER_SHARD` (4) / `KEIBA_SCORE_MAX_SHARDS` (4) / `KEIBA_SCORE_MIN_HORSES_FOR_PARALLEL` (8) / `KEIBA_LLM_MAX_CONCURRENT` (5, プロセス横断の claude -p 同時数上限=file-slot semaphore, fail-open)。**注意**: 検索バジェットを使い切るには score 帯を早める必要 (`--score-window/-tolerance` 例 9/5)。timeout (`KEIBA_SCORE_TIMEOUT` 既定900s) の 60% が research・残りが scoring。`tests/test_score_parallel.py` で prompt 形/merge/gate/fallback を検証。**Web UI からも設定可** (watch-auto 開始パネル「Claude 指数の検索を並列実行」トグル `score_parallel` + 「score 検索クエリ数/馬」`score_queries_per_horse`) → API (`WatchAutoStartRequest`) が `_start_locked` で env `KEIBA_SCORE_PARALLEL`/`KEIBA_SCORE_QUERIES_PER_HORSE` に変換し全 dispatch subprocess へ伝播 (bankroll/mode と同パターン・config persist/resume 対応)。変更はループ再起動で反映 (spawn 時に env 固定)。**レース予測分析タブ (score 専用) からも per-job 指定可**: 並列トグル + 「検索クエリ数/馬 (上限・回数)」+「締切 (秒)」を `AnalyzeRequest` (`score_parallel`/`score_queries_per_horse`/`score_timeout`) で受け、`JOBS.new(env_extra=)` がその analyze subprocess **だけ**に `KEIBA_SCORE_PARALLEL`/`KEIBA_SCORE_QUERIES_PER_HORSE`/`KEIBA_SCORE_TIMEOUT` を注入 (os.environ を汚さず per-job 隔離。OFF は `KEIBA_SCORE_PARALLEL=""` で継承値を打ち消す)。
     - **指数出力時に予想履歴詳細を早出し (ユーザ指示 2026-06-13)**: 指数キャッシュ後、4経路すべてが **early return せず**そのまま fall-through して `data/predictions/<race_id>.json` の **暫定 snapshot (`stage="score"`)** を保存する (3連単買い目の Claude 選定は bet 段のみ = score 段は機械フォーメーション)。これで Claude 指数が出た段階で Web UI の履歴一覧・詳細に「暫定」バッジ付きで出る。bet 段が締切直前に fresh odds で再計算し `stage="bet"` の確定版で上書きする。**実弾 enqueue は auto_watch の bet phase のみ**が行うので score の暫定 snapshot で賭けは飛ばない。`api/store.py` は `stage="score"` のまま残った snapshot (= bet 未発火で賭けていない) を `backfilled` と同様 **ROI 計測の見送り扱い** (participated=False・mode 別分母からも除外) にする。`scrape_*` の CLI で `--snapshot` 無し (save_snapshot=False) のときは従来どおり score で early return。
   - **bet ステージ** (締切1-2.5分前): `_load_llm_scores` で指数を読み `estimate_probs(llm_win_index=..., llm_blend=0.75)` に渡す → **まず model fundamental を市場とブレンド (β=0.78) し、その市場アンカー済 win に対し後段で** `ev._combine_llm_index` が `softmax(指数/T_LLM=25)` を loglinear 合成する (2026-06-13 ユーザ指示「最終Pで Claude ~75%」で**合成順を market→Claude に変更**)。最終Pの per-horse 実効重みは Claude=w / 市場=(1-w)·β / モデル=(1-w)·(1-β) (w=llm_blend·support_mult)、例 w=0.75・β=0.78 → **Claude≈75% / 市場≈19.5% / モデル≈5.5%**。Claude が触れていない馬・指数キャッシュ無しレースは w=0 で市場アンカー (β=0.78) のまま = 「市場無視 (β低) → 一様縮退 → 最長オッズ自動購入」事故ゾーンに入らない (β を一律下げる方式との違い)。最新オッズで束→enqueue→自動購入。
   - **picks/cuts 選定は撤去** (`_validate_and_update_bundle`/`_spawn_hit_bundle_claude` は bet 経路で呼ばない)。買い目は合成済 probs から `build_bundle` (joint Kelly + トリガミ防止) が決める。
   - **2段の配線**: `auto_watch._run_phase` が score/bet を**別 dedup 名前空間** (`auto_watch_analyzed_score.txt` / `..._bet.txt`) で2回実行。`--score-window/-tolerance` (既定5/2) と `--window/-tolerance` (bet 既定1/1.5)、`--llm-blend`。Makefile は `BAND_ARGS`。
   - **フォールバック**: 指数キャッシュ無し (score 未完/間に合わず/`--no-llm`) → `estimate_probs` の合成が no-op = モデルのみで bet (従来挙動)。snapshot に `llm_win_index`/`llm_blend`/`llm_scored_at`/`llm_fallback` を残す。
   - **要チューニング**: `T_LLM` (指数→確率の鋭さ。生 0-100 softmax は過尖鋭化するので温度で平坦化) と `LLM_BLEND_DEFAULT` (=0.75, 市場ブレンド後の Claude 合成重み) は `ev.py` 定数。arm 前にレース蓄積で sweep 推奨。**注意**: 旧記述「指数 vs モデル / 既定 0.5 / fundamental 段で合成」は 2026-06-13 に廃止 (合成順を market→Claude に変更し既定 0.5→0.75)。EV束 (`recommended_bundle`) の probs も同じ `estimate_probs` 出力を使うため、Claude スコア済の馬では EV束も最終Pが Claude 主導 (~75%) になる (市場アンカーは未スコア馬・指数無しレースにのみ残る)。
6. **投票束の切替 (2026-06-10〜, env `KEIBA_BET_BUNDLE`, 既定 `ev`)**: 実弾投票束は **`ev` (EV束 = `recommended_bundle`) と `trifecta` (3連単束 = `recommended_bundle_t`) を切替可能**。Web UI 開始パネル「投票束」Select → API (`bet_bundle`, Literal 検証) → env で auto_watch (enqueue 判定 `_bundle_not_bettable`) と投票 daemon (oddspark/ipat, req の `bundle_source` が env より優先) に伝播。
   - **既定 `ev` の根拠 (2026-06-10 実測)**: 3連単束は全系列 ROI 12-83% / Claude 選定脚 flat 44% と -EV 確定。修正後 EV束は全脚が「ドリフトシェード込み P×O≥1.02 + px_o≤2.0 + ½Kelly + トリガミ防止」を通過した時のみ legs が立つ = **大半のレースは見送り (それが正しい挙動)**。+EV 未実証なのは同じだが、唯一「買う前に毎脚の採算ゲート」を通る束。EV束の1レース予算は env `KEIBA_EV_BANKROLL` (`_ev_bankroll`, 既定 ¥10,000・UI 初期値は計測モード ¥5,000)。EV束に Claude 指数ゲートは無い (legs 非空のみ)。**注意 (✅ 2026-06-12 追従済)**: ダッシュボード (api/store.py) も `backfilled` 束を見送り扱いに修正し report と同 semantics になった (race 行に `bundle_backfilled` フラグ、UI はグレー表示)。詳細系列分析は引き続き `scripts/bundle_calibration_report.py`。resume の旧 state (bet_bundle キー無し) は旧挙動 trifecta を維持。
   - 旧「回収優先AI」(claude -p による EV束 picks/cuts 選定 = `select_bundle_stream`/`_validate_and_update_bundle`) は撤去済 (2026-06-06)。以下は `trifecta` 選択時の挙動:
   - **モード (`hit` のみ・recovery は廃止 2026-06-21)**: 3連単束は **全力的中モード (hit) が唯一のモード**で常に動く。除外なし・Claude 指数ドリブン・Kelly+トリガミ防止配分。かつて存在した「回収モード (recovery / 市場1番人気を1着除外 + 全オプション ¥100 均等買い)」は実測 ROI が hit (claude 49-83%) より大きく劣り (claude 14-16%、全オプション均等買いが最も出血) **コードから完全削除**した。`--t-mode` / env `KEIBA_TRIFECTA_MODE` / Web UI の「3連単束モード」Select などモード切替の配線も撤去済。
   - **組み方**: score ステージの Claude 指数を ranking に、bet ステージ締切直前に `_claude_select_trifecta` (`llm.select_trifecta_stream`, 検索なし高速・**市場無視** = 単勝オッズをプロンプトに渡さない) が3連単買い目を選定 → `build_trifecta_from_keys` (トリガミ防止つき)。失敗/間に合わず/keys 空なら `build_trifecta_hitmax` の機械フォーメーション (1着 `--t-head-max` / 2着 `--t-mid` / 3着 `--t-tail`) にフォールバック。
   - **1レース購入予算**: `--t-bankroll` / env `KEIBA_TRIFECTA_BANKROLL` (旧 `KEIBA_PLAN_T_BANKROLL` も互換で読む) / 既定 ¥10,000 (`_trifecta_bankroll`)。Web UI からは `trifecta_bankroll` で全 dispatch subprocess に伝播。
   - **Claude 指数ゲート (trifecta 束のみ)**: 指数キャッシュが無く model ランキングへ縮退した束 (`rank_source != "claude"`) は **enqueue もカート投入もしない** (auto_watch / oddspark_bet / ipat_bet の二重ガード、ユーザ指示 2026-06-03)。計測上も**見送り**として扱う (api/store.py, 2026-06-07)。EV束にはこのゲートは適用しない (市場+モデル駆動のため)。
   - **EV束 (`recommended_bundle`) はモデルのみの参考値**として snapshot/ダッシュボードに残す (3連単束との比較計測用)。投票には一切使わない。

snapshot に保存される主要フィールド:
- `horse_aptitude`: 各馬の指数 + 内訳 (total 降順)
- `aptitude_top_horses`: Plan G の集合
- `plan_a_keys` / `plan_b_keys` / `plan_c_keys` / `plan_g_keys` / `plan_h1_keys` / `plan_h2_keys` / `plan_f_keys` (3 連単)
- `bet_tables`: 単勝 / 複勝 の EV top 30 (馬連/ワイド/馬単/3連複 は実オッズが取れず無効 = 空。`build_all_bet_tables` は `rd.other_bets` の非空 type のみ出す)
- `bet_tables_g`: 各 bet type の Plan G picks
- `recommended_bundle_t`: **3連単束 (実弾投票対象, 固定)**。Claude 指数ドリブンの3連単のみ・市場無視・**全力的中モード (hit) のみ・Kelly+トリガミ防止配分**。`rank_source` ("claude"/"model") と `llm_select` (Claude 選定時の summary/confidence/n_keys) を持つ。`trifecta_keys` / `trifecta_params` も併存 (旧 snapshot は `plan_t_keys` / `plan_t_params`)。旧 snapshot に残る `mode`/`excluded_head`/`market_favorite`/`favorite_claude_index`/`stake_mode`/`flat_stake` は廃止された回収モードの痕跡 (現行コードでは生成しない)。
- `recommended_bundle`: EV束 = 全 bet type 横断の **joint (同時) Kelly 最適まとめ買い束** (`src/portfolio.py`)。レースの完全な top-3 結果分布 (全 ordered triple, Σp=1) 上で束全体の E[log(資金)] を最大化した成長率最適配分。独立 Kelly の単純和ではなく相関・排他性を考慮。+EV (シェード込み P×O≥1.02, px_o≤2.0, ½Kelly) が無ければ legs 空 = 見送り。**2026-06-10 以降は `KEIBA_BET_BUNDLE=ev` (既定) の実弾投票束** (2026-06-06〜10 の間はモデルのみの参考値だった)。予算は env `KEIBA_EV_BANKROLL`。
  - **トリガミ防止 (安全マージン付き)**: `odds×stake < 投資総額 × TORIGAMI_MARGIN` の脚を除去 → 残脚で再最適化を収束まで繰り返す。`min_payout_ratio ≥ TORIGAMI_MARGIN` を保証。**margin=1.10** (`src/portfolio.py`) は「束を組んだ時点のオッズ」からの**下振れ緩衝**: 締切直前ドリフトや複勝のレンジ幅で実払戻が下振れしても、~9% までは収支マイナスにならない (margin=1 では保存オッズでしかトリガミ無を保証できず、実オッズ乖離でトリガミ化していた)。`dropped_torigami` に除外数、`torigami_margin` も snapshot に保存。
    - **レンジ型 bet の下限採用**: 複勝は `fuku_min` (下限) を採用 (実払戻 ≥ 下限で確定 → トリガミ保証が崩れない)。これと margin の二段構えで「オッズ乖離 → トリガミ」を防ぐ。
    - **束に乗る bet type は odds 源によって異なる**:
      - **netkeiba 経路**: 単勝/複勝/3連単 のみ。馬連/ワイド/馬単/3連複 は netkeiba 側で誤オッズが出るため `fetch_and_parse(with_pair_bets=False)` で disabled (上記参照)。
      - **keiba.go.jp / JRA 経路 (NAR 自給・JRA 自給)**: **全7券種**を組合せ明示で取得できるため pool に投入。実機ではこの場合 **ワイド/複勝 (場合により馬連/3連複) が支配的**になる。`build_bundle` の pool 選抜が `_kelly_ind=(px_o-1)/(odds-1)` 降順で `max_legs=12` に絞るため、`odds` が桁違いに大きい3連単 (典型 1000-10000倍) は `_kelly_ind` が 0.01-0.1% と極小で top-12 圏外 → 候補から外れる。3連単 +EV 候補は `rows` に数百行残るが束には乗らない、というのが現状の数学的帰結 (= joint Kelly が log-wealth を最大化する最適解)。トリガミ防止 (margin=1.10) もワイドのような hit 率の高い券種を優遇する方向に働く。3連単をもっと束に入れたい場合は `max_legs` の引き上げか pool 選抜基準 (px_o 降順等) の変更が必要。
  - **claude -p による EV束 picks/cuts 選定 (回収優先AI) は撤去 (2026-06-06)**: `llm.select_bundle_stream` / `analyze._validate_and_update_bundle` / `parse_bundle_review` / `scripts/validate_bundles.py` は削除済。Claude の役割は score ステージ指数 + 3連単買い目選定に特化。古い snapshot の `llm_review` (validated バッジ) は記録として残る。
    - **締切前の再判定**: watch-auto の bet 帯は**最新オッズを再取得して estimate_probs〜束生成まで通す** (= 最新オッズでの最終判定)。検索基準を発走→締切に切り替えたのは、レーススケジュールが変動しても「賭けの締切までの lead time」が安定するため。netkeiba/keiba.go.jp/JRA/oddspark いずれの経路も dispatch 時に fresh fetch する。締切=`parse.close_at_for_start(start_at)` で `start_at - CLOSE_LEAD_SEC(=120秒)` で固定。
  - frontend (履歴詳細ページ最上部) は full Kelly を表示しつつ ½ Kelly を実運用推奨として併記 (楽観バイアス対策)、的中時払戻・min_payout_ratio (目標 ≥×margin で色分け)・検証バッジ/調査メモも表示。古い snapshot は欠落 → 近似 Kelly ランキングに fallback。`scripts/backfill_bundle.py` で後付け (start_at/close_at も再パース補正)

**オッズ変動の時系列キャプチャ (`src/odds_timeline.py`, 2026-06-06〜)**: score 段 (締切5-7分前) と bet 段 (締切1-2.5分前) で取得済みの fresh odds を**追加 fetch ゼロ** (= netkeiba rate-limit リスクなし) で `data/cache/odds_timeline/<race_id>.jsonl` に append する。hook は `_run_score_stage` 冒頭 (LLM 可否チェックより前) と `_save_prediction_snapshot` 冒頭の2点で、netkeiba/JRA/keibago/oddspark 全4経路共通。result fetch の `final_odds` (束の脚のみ) と合わせて1レース最大3点の時系列になる。同一オッズ行は odds_hash で dedup (netkeiba 経路 score phase は score 後そのまま snapshot 保存に fall-through するため)、capture 失敗は例外を呑む (解析を止めない)。用途: ①締切直前ドリフトの実測 → `TORIGAMI_MARGIN` の券種別較正 ②late-money momentum (score→bet のオッズ変化) の特徴量化検証。一次分析 (final_odds 150 件 / 束の脚 126 本): 3連単の 最終/bet時 オッズ比 median 0.957・p5 0.38 で、44% が margin 1.10 を食い破る下振れ → NAR 小 pool での自票インパクト疑いも要検証。
- **Step 2 = poll daemon (`src/odds_capture.py` / `make odds-capture`)**: 締切前 `--window` 分 (既定30) に入ったレースを `--capture-interval` 秒おき (既定180) に polling して同じ timeline に stage="poll" で append。**netkeiba は使わない** — NAR は keiba.go.jp (`find_keibago_race`+`fetch_keibago_bets`, 静的GET全6券種)、JRA は JRA 公式 (`find_jra_race`+`fetch_jra_bets`, POST chain 全7券種、token 使い捨て前提で毎回 find から walk)。discovery は watch-auto と共通の `auto_watch.discover_today_races` (oddspark NAR list + 競馬ブック×JRA公式 join、`_list_due_races` から抽出したもの)。ファイル名は `_normalize_race_id` で predictions/results と同じ join key。失敗/同一オッズは 300s cooldown。watch-auto とは独立プロセスなので投票 dispatch latency に影響しない。実機確認済 (佐賀12R 全6券種、組合せ数整合)。

## 2026-06-10 全面レビューの実測結論 (最重要 — 戦略判断はここを起点に)

snapshot 349 × results 324 の突合 + live 蓄積データの MLE (multi-agent レビュー + 検証) で確定した事実:

1. **勝率次元でモデルは市場に勝てていない**: Benter 型 conditional logit (α,β **自由**) の MLE (`scripts/fit_blend_mle.py`) は **α≈0 — モデル成分の独立情報は統計的にゼロ**。⚠ 2026-06-11 第5R 補正: 旧計測 (α=0.016, N=324) は **β=0.78 era (〜06-01) の市場ブレンド済み snapshot を「市場フリー fundamental」として混入**していた。era フィルタ修正後のクリーン計測は α=-0.013 (95%CI [-0.45, +0.55]), β=1.003 — 結論は同方向だが N 減で CI 拡大。勝者 log-loss も market 単独 1.585 < model+market 1.617 (`scripts/validate_claude_value.py`, 同 era フィルタ修正後・汚染 41 件除外。旧数字 1.601/1.626/1.629 は汚染込み)。「モデルと市場の乖離 (px_o>1) を買う」は構造的に adverse selection。
2. **全実弾系列が大幅 -EV** (実測 ROI, `scripts/bundle_calibration_report.py`): **EV束 51.5% (n=269)** ⚠ 2026-06-11 第5R 補正 — 旧 66.9% は backfill_bundle が後付けした **paper 束 (実際には賭けていない, 単体 ROI 247.5%) が混入**した上振れ。`backfilled` フラグで系列分離済み。3連単束 claude 旧hit 82.8% (n=57) / 新hit 11.9% (n=18) / Claude 選定 3連単脚の flat ¥100 でも **44.0%** (2,715脚) — 選定自体に正の edge は無い (flat でランダム以下)。
3. **市場アンカー型クロスプール (Dr.Z 系) も現データでは -EV** (`scripts/backtest_market_anchor.py`): 単勝アンカー × Discounted Harville (λ を live 結果で MLE 較正: λ2=0.68/λ3=0.62 でも) px_o>1 の複勝/3連単 flat 買いは NAR 3連単 ~27% / 複勝 ~48-64%。**閾値を上げるほど悪化 = 高 px_o はチェーン側の誤り**で、NAR の exotic pool は単勝外挿より正確だった。snapshot の `market_anchor_ev` でペーパー計測は継続 (JRA は n 不足で未結論 — pool が深い JRA で要観測)。
4. **修正済の重大バグ** (commit ce8cb64): ①de-vig (power_method_overround) が正規化済み入力で k=1 恒等写像の no-op ②live β=0 (市場無視実験) は past_runs 欠損時に一様分布へ縮退し **EV=odds/n で最長オッズを自動購入** ③EV/Kelly/トリガミが bet 時オッズのままでドリフト未補正 (的中時 place median 0.891) ④full Kelly がコード強制 (½Kelly は表示のみ) ⑤3連単控除率の 22.5% 誤記 (正: 27.5%)。
5. **学習系の構造問題 (✅ 2026-06-12 修正済)**: train/valid の「時系列 split」が実際は **会場コード split** だった (`train.py _race_id_to_unix` が race_id の int ソート → 年内は会場コードが上位桁。valid = 帯広ばんえい645+佐賀+高知のみ・期間は train と完全重複)。**修正内容**: dataset に `race_date` 列 (NAR=rid 由来 / JRA=HTML title 由来, 100% 充足) → train/_make_splits/eval_holdout/sliding_window_eval/train_segment_models 全て日付ソート化 (race_date 欠落 parquet は明示エラー)、ばんえい (帯広65) をグローバル/NAR から除外し **第3セグメント `lgbm_banei.txt`** として分離 (`ev.segment_of_rd` が venue 65 → banei)、Data05 新 semantics で par→dataset→全モデル再生成済。**クリーン再較正値 (2026-06-12)**: グローバル T=0.75 / jra T=0.30 β=0.900 λ2=0.880 λ3=0.726 / nar T=0.60 β=0.902 λ2=0.746 λ3=0.551 / banei T=0.75 β=0.902 λ2=0.771 λ3=0.552。`make retrain` で一括再生成可。

**運用指針 (実測が覆るまで)**:
- 3連単束の実弾は計測モード (`KEIBA_TRIFECTA_BANKROLL=2000` 程度) に下げ、`bundle_calibration_report.py` の rolling ROI が 100% を超える系列が出るまで上げない。全系列 -30%〜-85% の現状で ¥10,000/R は確定的資金流出。
- 定点観測 (全て読み取り専用・常設): `bundle_calibration_report.py` (週次: 系列ROI/楽観係数/ドリフト→DRIFT_SHADE 較正) / `fit_blend_mle.py` (+100レース毎: α が CI で 0 を離れたら model に意味が出た合図) / `backtest_market_anchor.py` / `validate_claude_value.py`。
- 新規 edge 候補の優先順: ①**odds_timeline の late-money momentum** — ✅ 一次検証済 (2026-06-12, `scripts/backtest_momentum.py`, n=85): 買い側は有意でない (最短縮馬 z=+0.68, ROI 133%→単発抜きで~101%) が、**ドリフト側 (r=bet/score>1.05) は全バンド一貫して最悪** (NAR flat ROI 16.6% / 人気薄 34.5% / 的中率も単調減少) → **買いシグナルでなくカット/シェードフィルタ候補**。snapshot に `late_money` (score→bet 単勝オッズ比) を自動記録中 — ~300 レース蓄積で再検定し、ドリフト脚 cut の paper 検証へ ②JRA クロスプールのペーパー継続 ③(✅ 済) 日付 split 修正 + ばんえい分離 + 再学習。
- **再学習の宿題 (✅ 2026-06-12 消化)**: Data05 解釈修正 + surface 語彙統一 + 日付 split 修正 + ばんえい分離を反映して `make retrain` (par 再集計 → dataset 再生成 → グローバル+3セグメント再学習 → eval_holdout) を実行済。較正値は上記レビュー結論 5 を参照。今後 parse 系を直したら `make retrain` を回すこと (全体 ~40 分, scrape 不要)。

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

### 唯一 robust に確認された「対市場 +7-8pt」設定: 単勝 β=0.78 (絶対 ROI は <100% に注意)

🚫 **2026-06-12 棄却**: 日付ベース split + ばんえい分離 + Data05 新 semantics での再評価 (`make retrain` 内 eval_holdout, valid=20260502-0528 の真の時系列 OOS n=1274) で、**β=0.80 peak ROI 81.5% = 市場比 +0.92pt / β=0.78 で +0.46pt** に縮小。下表の「+7-8pt」は会場コード split の幻影だったと確定 (valid が佐賀/高知/帯広に偏り、そこでだけモデルが市場に勝って見えた)。セグメント別ホールドアウトでも jra +0.3pt / nar -0.8pt / banei -2.3pt。**勝率次元でモデルは市場とほぼ同等 (僅差) が正しい現状認識**。なお β MLE はクリーン split で ~0.90 (旧 0.9999 の boundary 張り付きから変化 — モデル成分はゼロではないが微小)。

⚠ **2026-06-10 補正 (旧)**: この節の「+EV」は誤解を招く表現 — β=0.78 は**市場 (β=1.0) に 7-8pt 勝つ**が、絶対 ROI は 88-96% で**控除 (20%) には負けている** (= 打ち続ければ -4〜-12%)。また下記の W3/W4 は会場コード split (上記レビュー結論 5) 上の評価で、時系列 OOS ではない点も信頼度を下げる。live N=324 の MLE では β≈0.95 が示唆されている。

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

このリポジトリの `claude -p` 評価セッションでは **Tavily MCP** が利用可能 (Brave Search は 2026-06-12 に廃止、Tavily に一本化)。

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

- 1 レースあたり **頭数 × `KEIBA_SCORE_QUERIES_PER_HORSE` クエリ** (env 未設定の単一セッション既定 2/頭、Tavily 合計、`llm.py` の score プロンプトと同基準)。**`KEIBA_SCORE_PARALLEL=1` 時はシャード並列**で 頭数 × `KEIBA_SCORE_QUERIES_PER_HORSE` を分散実行 (shobu 一括生成は 10/頭 = 頭数×10、上記 score ステージ参照)
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
