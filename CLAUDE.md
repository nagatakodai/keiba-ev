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

**watch-auto への統合 (block 中も自動継続)**: netkeiba 両ドメイン block 時、`auto_watch._list_due_races` は **oddspark で NAR race discovery** (`fetch_race_list_oddspark`: KaisaiRaceList→OneDayRaceList で当日全 NAR の race_id + 発走時刻) に fallback し、該当レースは `source="oddspark"` を付けて `_dispatch_nar_fallback` で dispatch する → **keiba.go.jp を優先** (`_dispatch_keibago`: `python -m src.scrape_keibago <rid> --snapshot --start-at=<unix>`、全6券種)、keiba.go.jp が解決できない場のみ **oddspark にフォールバック** (`_dispatch_oddspark`、単複/3連単)。トリガミ防止束を含む snapshot を保存 (`odds_source="keibago"`/`"oddspark"`, 発走時刻も補完)。これで規制中でも NAR の watch-auto が止まらず 3連単的中モードの束を出し続ける (keibalab は当日一覧が JS 化で discovery 不能なため最終 fallback)。claude 考察 (score ステージ各馬指数 + 3連単買い目選定) も oddspark 経路を含む全経路で netkeiba 経路と同じ関数で実行され (`--phase=score|bet`)、履歴・snapshot も同形式で作られる。

**block 中の result fetch (`fetch_result.process_pending`)**: 結果取得は第一に netkeiba。失敗理由が block (`NetkeibaBlocked`/空 body/CloudFront) で、かつ **NAR レースなら keiba.go.jp で確定結果を fallback 取得** (`scrape_keibago.fetch_keibago_result`: RaceMarkTable の着順表 + RefundMoneyList の組番一致3連単配当) → そのまま save_result。これで **predict (keiba.go.jp 自給) と同様に result も自給**でき、netkeiba block 中でも NAR は watch-auto→snapshot→結果取得→calibration の loop が完結する (実機確認: 金沢12R で netkeiba block→keiba.go.jp fallback→着順8-3-6/3連単6030円を save、calibrate に反映)。keiba.go.jp でも取れない (JRA / 当日外 / 未確定) ときは従来どおり **attempt を消費せず `BLOCK_RETRY_INTERVAL_SEC` (15分) 間隔で pending を維持**し解除後に取得 (block でない通常の失敗は max_attempts で failed)。`fetch_keibago_result` は当日確定レースのみ (TodayRaceInfo ベース)。

**結果の自動取得ループ (`api.main.ResultAutoFetcher`, make api 稼働中, 2026-06-20 ユーザ指示)**: 「make api 実行中は予測分析履歴の結果を取り続けて」に対応。API に常駐 asyncio ループを持たせ、既定 **10 分毎** (2026-06-28 ユーザ指示で 5→10 分・env `KEIBA_RESULT_FETCH_INTERVAL_SEC`) に ①**発走済・結果未取得の全予測** (日付不問・2026-06-28 ユーザ指示で「本日分のみ」→「全レース」に拡大、`list_predictions`) を `fetch_result.schedule(..., resurrect_failed=False)` で pending に enqueue (内部 race_id→netkeiba rid 復元・既存結果は no-op・dedup) ②`process_pending` で確定結果を取得 → `data/results` 保存 → calibrate / 予測分析履歴 / ダッシュボードに反映。**watch-auto を回していなくても** (手動 analyze / 勝負レース由来の予測も含め) 結果が埋まり続ける。発走前 (`start_at` 無し含む) は対象外なので enqueue 件数は数十程度に収まる (実測 ~39、holdout backtest は start_at 無しで除外)。`resurrect_failed=False` により **terminal failed (中止/欠落で恒久取得不能) を毎 tick 復活させない** = 無限リトライ + netkeiba 過負荷を回避 (block 失敗は `process_pending` が attempt を消費せず pending のまま 15 分間隔で retry)。`process_pending` は file-lock 済なので watch-auto と併走しても二重 fetch しない。blocking IO は `asyncio.to_thread` でオフロード。lifespan startup で起動・shutdown で停止。状態は `GET /api/results/auto`。`tests/test_shobu_auto.py::test_results_auto_enqueue_filters`。

**保存基準 (GCS/BigQuery 移行)** — 現状 raw HTML ~2.3GB / parquet <1MB なのでローカル維持で十分。**数百 GB を超える前に GCS bucket + BigQuery テーブルに移行**する想定 (新規スクリプトを書いて parquet を bq load → SQL で集計、raw HTML は GCS にミラー)。

**今日の勝負レース (`src/shobu.py` + Web UI `/shobu`, 2026-06-20 ユーザ指示)**: 当日の全レースを取得し「勝負レース (= 通常より賭ける価値が高そうなレース)」を抽出する Web ページ。ボタン 1 つで `discover_today_races` (netkeiba 非依存) → 各レースを **2 基準** で採点する:
- **(A) 強弱がはっきり** = 市場の単勝 implied 勝率分布の集中度 `sep_score = 100·(1 − 正規化エントロピー)`。一様フィールド ~0、1〜数頭が突出すると高い。データ源は **最新オッズの軽量 fetch** (単勝のみ・NAR=`fetch_keibago_win_list` 1 GET / JRA=`fetch_jra_win_list` 1 POST、netkeiba 不使用) か、既存 snapshot の `market_win_index` 復元 (`(idx/100)^1.5` で implied に戻す = market_win_index と同尺度で整合)。
- **(B) 市場との順位乖離** (2026-06-20 ユーザ指示で「単に Claude>市場」から変更) = 各馬を Claude 指数 / 市場指数でそれぞれ降順ランク付けし、`rank_gap = market_rank − claude_rank` (正 = Claude が市場より上位評価 = 市場過小評価) を見る。「市場2番人気なのに Claude 本命」= その馬の rank_gap=1。**乖離馬** = rank_gap≥1 かつ 指数差≥`edge_margin` (順位だけでなく数値の裏付けも要求)。**score** = `top_rank_gap·20 + Σ_edge(rank_gap·5 + max(0,指数差)·0.4)` (top_rank_gap = Claude本命の市場順位−1 が主軸)。`edge_threshold` 以上で合格。指数は既存 snapshot の `index_compare` (無ければ `llm_win_index`/`market_win_index` の両方) から取り、両指数が揃う馬が2頭未満なら評価不能。**基準 B は既存スナップショット中心** (無料・即時)。ボタン押下で **全レースの Claude 指数を一括生成** (claude_all 既定ON, claude -p が Tavily/WebFetch で各馬を web 検索 → 指数 → snapshot)。OFF にすると `claude_eval N` で強弱上位 N 件のみ新規生成。
- **option (CLI / Web UI 両対応)**: 基準 A/B の ON/OFF・合成 (OR/AND)・しきい値 (sep_threshold / edge_threshold / edge_margin)・対象 (all/jra/nar=地方平地/**banei=帯広ばんえい**, `ev.segment_of_rd` と同じ3区分で分離)・発走前のみ・最新オッズ取得・Claude 一括生成 (claude_all) / 上位N件 (claude_eval)。`shobu_score = max(active) + 0.25·min(active)` でランキング。UI は推奨を専用「勝負レース(推奨)」セクション (緑カード+番号) に分離し、推奨外は折りたたみ。
- **配線**: `POST /api/shobu/scan` (ShobuScanRequest) が `build_shobu_cmd` で Job を起動 (analyze と同じ subprocess+SSE log 機構) → 結果を `data/cache/shobu/<date>.json` に atomic 書き出し → `GET /api/shobu/result?date=` が配信。frontend は Job 完了を polling して result を再取得。各レース行は `/predictions/<race_id>` (内部 race_id join) へリンク。`tests/test_shobu.py` / `tests/test_shobu_auto.py`。
- **勝負レースの最新オッズ自動更新 (2026-06-21 ユーザ指示で方針転換)**: 勝負レースページを開いている間、**2分毎に推奨 (勝負レース) のみ最新オッズで再採点**する (`POST /api/shobu/refresh` → `shobu.refresh_recommended`)。各推奨レースの単勝を 1 fetch (NAR=keiba.go.jp / JRA=公式・netkeiba 不使用) し、強弱 (基準A) と 市場乖離 (基準B = `market_index` を最新オッズで `_market_index_from_odds` 再計算、Claude 指数は snapshot 据え置き) を recompute して勝負スコアを更新。**discovery も Claude -p も呼ばない**ので即時・規制リスク無し。勝負スコアの履歴を `data/cache/shobu/<date>.scores.json` に追記し、前回比 (`score_delta`) と時系列 (`score_history`) をレースに付ける → UI が ▲▼ デルタ + 極小スパークラインで表示。採点ロジックは scan と共通の `_evaluate_race` / `_build_summary` に切り出し済 (`claude_use_fresh_market=True` で基準Bも最新オッズ化)。フロントは `web/app/shobu/page.tsx` の 2分 interval (トグル「2分毎に最新オッズ更新」既定 ON・「今すぐ更新」ボタン併設)。**全レース再スキャン・Claude 再生成は従来どおり手動ボタン (`POST /api/shobu/scan`) のみ** (母集団更新や Claude 生成は重いので自動化しない)。`python -m src.shobu --refresh --date <YYYYMMDD>` で CLI 再採点も可。※ 旧方針 (2026-06-20「自動更新はしない」) はこの指示で撤回。
- **Claude 指数一括生成の並列度と keiba.go.jp レート制限 (2026-06-21 rc=1 調査)**: 一括生成は各レースを `python -m src.scrape_keibago/jra <rid> --phase=score` の **別 subprocess** で走らせ、`ShobuScanRequest.claude_eval_parallel` 個を ThreadPool で同時起動する。**keiba.go.jp / JRA公式は 1 IP からの同時アクセスをレート制限**し、~20 並列でスクレイプすると odds が**空**で返り → `analyze_keibago` が `KeibagoError("オッズが空")` → `scrape_keibago` main が `SystemExit(1)` → score subprocess が **rc=1** で死ぬ (= shobu ログの `[claude-eval] ... rc=1`)。結果 **Claude 指数がつかない**。実機確認: 21 並列スクレイプで全件 win 空 + 以後の sequential も一時ブロック。→ **across-race 並列 (`claude_eval_parallel`) は既定 4 に抑える** (keiba.go.jp を bursting しない)。深い検索の「20並列」は `KEIBA_LLM_MAX_CONCURRENT=20` (claude -p 同時数=keiba.go.jp 非依存) + `score_parallel` の per-race シャードが担う。`rc=1` を見たら claude_eval_parallel を下げる (or 時間を置いて再実行)。
- **検索クエリ数とシャード飽和 (2026-06-28 ユーザ指示)**: shobu の Claude 指数は **1頭につき10クエリ = 頭数×10 クエリ** を流す (`ShobuScanRequest.score_queries_per_horse=10`・CLI `--score-queries-per-horse 既定10`)。`_run_claude_eval` が score subprocess に env を渡す: ①`KEIBA_SCORE_QUERIES_PER_HORSE=10` (並列パスは `_shard_numbers` が全馬を被覆するので合計=頭数×10、単一セッション=`score_horses_stream` も同 env を読むので **<8頭の小頭数でも頭数×10**) ②**並列飽和**: across-race=`claude_eval_parallel` に対し シャード/レース = `KEIBA_LLM_MAX_CONCURRENT // claude_eval_parallel` (既定 20//4=5)・`KEIBA_SCORE_HORSES_PER_SHARD=3` で各レースを細かく刻み、claude -p 同時実行を上限近くまで埋めて research を速める (per_shard を小さく=各シャードの担当頭数減=速い)。シャードを増やしても scrape は across-race=4 のままなので keiba.go.jp は bursting しない (claude -p は Tavily=別系統) ③`KEIBA_SCORE_TIMEOUT = min(max(120, claude_eval_timeout − 90), claude_eval_timeout − 10)` で内側 claude を外側 subprocess kill の手前に**必ず**収める (外側が先に発火すると score 結果が丸ごと失われ rc≠0 で指数なしになるため。timeout が小さくても反転しないよう二重に上限クランプ)。CLI `--claude-eval-timeout` は **下限 210s にクランプ** (内側 research 60s + scoring 60s floor + scrape が外側 kill より先に終わるのを保証)。operator が env で per_shard/max_shards/timeout を明示していればそれを尊重。`tests/test_shobu.py::test_run_claude_eval_*` / `tests/test_score_parallel.py::test_single_session_respects_query_env`。
- **長期 +EV を保証する指標ではなく「賭ける価値が高そうなレース」の screen**。
- **ダッシュボードの主役を「勝負レース仮想収支」に変更 (2026-06-21 ユーザ指示)**: 実弾投票束 (EV束/3連単束) の Live P/L 表示はダッシュボード (ホーム/確率較正) から撤去し、代わりに **Claude 指数上位 N 頭の3連単 BOX を仮定した「勝負レース仮想収支」** (`GET /api/shobu/pnl`, `api/store.py:compute_shobu_pnl`/`_shobu_box_size`、例 7頭立て=上位4頭 BOX) を主役表示にする。各レースの Claude 指数上位馬で BOX を組み、確定結果と照合した仮想 P/L を集計する (実弾の露出ではなく screen 指標の検証用)。
- **shobu 評価レース全体の仮想収支を別カードで併記 (2026-06-28 ユーザ指示)**: 「Claude 指数が全ての馬についていて結果があればダッシュボードに反映する」。勝負レース(推奨)収支とは**別カード (非破壊)** で、**推奨に限らず shobu が評価した全レース** (= 当日スキャンの母集団) の上位N頭3連単BOX 仮想収支を表示 (`GET /api/shobu/indexed-pnl`, `api/store.py:compute_indexed_pnl`)。**当初は `data/predictions` 全体を母集団にして 153 件に膨れた (betting pipeline の過去スコア ~06-12〜 が混入) が、ユーザ指摘「全レースがこんなに多いはずがない・ほとんど推奨のはず」で `data/cache/shobu/*.json` が評価したレース (recommended + 非recommended) に scope 修正** → 推奨カードの proper superset (実測 推奨47 ⊆ 全48・ほぼ推奨)。指数条件は推奨カードと同じ (BOX 可能=指数3頭以上+結果確定)。BOX/的中/配当は共通コア `_shobu_box_pnl(..., recommended_only=)` + `_box_race_pnl` に集約 (compute_shobu_pnl=recommended_only=True / compute_indexed_pnl=False、重複なし)。ダッシュボード (`web/app/page.tsx`) は hero (推奨) の下に `IndexedPnlCard` を併記。`api.indexedPnl()`。`tests/test_shobu_pnl.py`。



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
   - **score ステージ** (締切5-7分前): `llm.score_horses` (dispatcher) が Tavily/WebFetch で各馬の適性・状態・取消を調べ `data/predictions/<race_id>.llm.json` に指数キャッシュ。`analyze._run_score_stage` が4経路 (netkeiba/JRA/keibago/oddspark) 共通で呼ぶ。
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
