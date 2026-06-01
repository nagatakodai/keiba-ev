# 競馬で年間プラス収支 (真の +EV) を出している人々が実際に何をしているか — 文献調査 + 当 repo への適用

調査日: 2026-06-01 / 対象 repo: `/home/ryuryo/keiba-ev`
姿勢: 「必勝法」商材は無視。**実在の +EV 実績 (Benter / 香港シンジケート / Dr Z / CAW リベート / pool overlay)** と査読文献のみに焦点。当 repo の N=7,000 バックテスト結論「どの戦略も ROI<100%」を前提に、**数学的に正直に**何が残るかを書く。

---

## 0. 結論サマリ (先に)

1. **Benter が「市場+α」を出せたのに我々の market-as-feature 実験 (+2.8pt in-sample) が overfit になった核心的理由は、Benter が α/β を *fundamental モデルを凍結した別データ partition* 上で MLE 推定した点**。我々は β を valid set 上で sweep して peak を拾った → それは valid への overfit。**処方: 2 段を実装するなら β は「fundamental を学習した fold とは別の fold」で 1 回だけ MLE 推定し、以後固定する。** これは既存コード (`estimate_probs` の loglinear blend, `train.py` の時系列 split) のほぼ自然な拡張で実装できる。
2. **プロが単勝でなくエキゾチック (3連単等) を好む理由は「確率推定の質」ではなく「公衆が条件付き確率の合成を下手にやる」構造**。Benter 論文 (1994) が数値で示す: 単独では単勝 -EV の 2 頭 (E=0.955, 0.996) が、**馬連に組むと E=1.16 (+16%)** になる。"the more exotic the bet, the higher the advantage." ただしこれは **fundamental モデルが ordered/conditional 確率を市場よりよく出せている時のみ**成立する。我々の現状は「win 1着率は市場接近、しかし2着・3着条件付き分布は素の Harville+λ近似」なので、**3連単で勝てないのは確率推定 (特に条件付き2-3着) の質**が主因。
3. **CLV (締切直前オッズ) は parimutuel では特に強力**。Ziemba (2023): 現代 parimutuel は **「全賭け金の半分以上がレース発走中まで pool に記録されない」** (CAW がアグリゲータ経由で締切間際に流し込む)。当 repo の「締切1分前発火」(`bet_scheduler`) は正しい方向だが、**parimutuel では自分の見たオッズ ≠ 確定オッズ** という致命的な構造があり、これは「最終オッズで EV を測る」バックテストと「締切前オッズで賭ける」実戦の乖離を生む (後述の検証必須項目)。
4. **日本にはリベートが無い** → CAW シンジケートのビジネスモデル (break-even handicap + リベートで黒字) は **原理的に再現不可**。残るのは ①控除率の低い券種 (単複 20% / WIN5 30%) ②公衆の条件付き確率合成の下手さ (エキゾチック overlay) ③公衆バイアス (人気馬の過剰、3着スペシャリストの軽視) の 3 つだけ。
5. **「どうせ -EV なら全力で3連単を当てにいってよいか?」への正直な答え: NO (ただし条件付き)**。下記 §8。

---

## 1. Bill Benter / 香港シンジケート — 二段統合の正体

**一次資料**: Benter (1994) "Computer Based Horse Race Handicapping and Wagering Systems" (gwern.net/datagolf ミラー); Acta Machina の注釈版; Ziemba (2023) LSE "Pari-Mutuel Betting Markets: Racetracks and Lotteries Revisited"。

### 1.1 二段モデルの厳密な仕様

- **Stage 1 (fundamental)**: 馬の特性 (過去成績・正規化タイム・斤量・騎手・距離/馬場適性・**"過去出走数" など一見無意味な変数も大量に**) で multinomial/conditional logit を学習し win 確率 f_i を出す。
- **Stage 2 (combined, これが革新)**: f_i と **公衆オッズ暗黙率 π_i** を *log 空間で* 2 つ目の conditional logit に入れる:

  ```
  c_i = exp(α·log f_i + β·log π_i) / Σ_j exp(α·log f_j + β·log π_j)
  ```

  α, β は MLE 推定。「α, β は roughly モデルと公衆の *相対的正しさ*。α が大きいほどモデルが良い」(Benter)。

> **これは当 repo の `src/ev.py:estimate_probs` の `blend_method="loglinear"` と数式的に同一**:
> `logs[k] = alpha*log(f) + beta*log(pi)`, `alpha = 1 - market_blend`, `beta = market_blend` (ev.py:144-156)。
> **違いは β の決め方だけ** (§7 で詳述)。

### 1.2 なぜ公衆オッズを混ぜると激変するか — 我々の +2.8pt との違い

Acta Machina が抽出した Benter の R² (out-of-sample 系):

| モデル | R² |
|---|---|
| 公衆 (π のみ) | 0.1218 |
| fundamental (f のみ) | 0.1245 (公衆 +0.0027 のみ) |
| **combined (α log f + β log π)** | **0.1396 (公衆 +0.0178)** |

決定的な対照実験: **fundamental + 公衆 → ΔR²=+0.0090 に対し、新聞予想 (tipster) + 公衆 → ΔR²=+0.0002**。つまり「公衆と混ぜて伸びる」のは **fundamental が公衆オッズに *無い独立情報* を持っている時だけ**。新聞予想は公衆オッズに織り込み済みなので混ぜても伸びない。

**我々の market-as-feature 実験が in-sample +2.8pt なのに sliding-window で崩れた理由 (3 つ)**:
1. **β を valid で sweep して peak を拾った** = valid への overfit。Benter は α/β を fundamental 凍結後の **別 partition** で 1 回 MLE 推定し以後固定 (ピークサーチではない)。
2. **fundamental と combine を同じデータで学習すると leakage**。German-horse-racing の Benter 分析が明記: Stage 1 と Stage 2 は **データを分ける** (Stage 1 を train 全体で学習し、その予測を同じ train で combine 学習すると f_i が過学習した値になり β がそれに合わせてズレる)。
3. **データ量**。Benter: 開発に最低 500-1000 race、実運用は ~2,000 race + 5 年継続。我々の N=7,000 は足りる量だが、**JRA 1,410 / NAR 5,809 と分布が NAR ダート偏重** で、JRA への transfer が未検証 (CLAUDE.md の "NAR ダート bias 注意" と整合)。

### 1.3 ROI とリベートの役割

- Benter の現実的 ROI: **1 レース turnover の 0.25-0.5%** (無限 bankroll)、好条件でも **1.5% 超は稀**、起業時は **0.1-0.2%**。← つまり「市場を僅かに上回る」だけで、巨大 turnover を回して総額で稼ぐビジネス。
- 香港の当時: 平均 takeout **19%**, pool **>$10M/race** (低ボラ・締切オッズ安定)。
- Ziemba (2023): **シンジケートの黒字はリベートと、"全賭け金の半分以上が発走中まで pool に記録されない" 構造**に依存。Benter 本人も Dr Z 夫妻に place/show 系を電話で質問していた (黎明期)。

**→ 当 repo への含意**: 「市場を 0.5-1.5% 上回る」が現実的天井。これは Kelly で複利運用し巨大回数を回して初めて意味を持つ。**単発・少数レースで判断する限り永遠に検証できない** (CLAUDE.md の哲学と完全一致)。

---

## 2. 控除率 / リベート / プールサイズ — 日本で何が残るか

### 2.1 日本の券種別控除率 (出典: JRA 公式「馬券のルール」, netkeiba, 複数二次)

| 券種 | 払戻率 | 控除率 |
|---|---|---|
| 単勝 / 複勝 | **80%** | **20%** ← 最良 |
| 枠連 / 馬連 / ワイド | 77.5% | 22.5% |
| 馬単 / 3連複 | 75% | 25% |
| 3連単 | 72.5% | 27.5% |
| WIN5 | 70% | 30% |

→ **単複が最も控除率が低い** (Benter が place/show を「watered down」と嫌ったのは香港固有事情 = 香港に North American 式 place 無し・show pool 情報が off-track で取れなかったため。**日本では単複の低控除率は活かせる資産**)。

### 2.2 リベートの不在 (決定的)

- 米国 CAW (Computer Assisted Wagering) は **リベート 5-10%** で takeout を実効半減し、それで黒字化。Reddit/algobetting の実務者: **"present-day CAW's are successful strictly due to rebates, they'd be net losers without the rebates."**
- **日本 (JRA/NAR) には一般ベッターへのリベート制度が無い**。JRA スーパープレミアム等の「全券種 80% に引き上げ」キャンペーンが擬似的に控除率を下げる稀な機会だが恒常的でない。
- **→ 日本で残るエッジは「リベート以外の 3 つ」のみ**: (a) 控除率の低い券種を選ぶ, (b) 公衆の条件付き確率合成の下手さ (エキゾチック overlay), (c) 公衆バイアス (favorite-longshot, 3着スペシャリスト軽視, 騎手人気)。

### 2.3 プールサイズと「公衆と乖離する場所」

- プロが勝つのは「モデルが公衆と乖離する大穴・大プール」。理由: 大プールは自分の賭けがオッズを動かさず (Benter の "pool-size-limited" 制約が緩い)、かつ公衆の系統的バイアスが残る。
- **NAR は小プール** → ①自分の賭けでオッズが動く (CLV が自分に不利) ②`Thoroughbred Idea Foundation` が報告するように小プールは pool manipulation の温床 (43-1 の馬に締切30秒前に show 5,000ドル等)。**当 repo は NAR 偏重なので、小プールの「最終オッズが読めない」リスクが構造的に大きい**。

---

## 3. エキゾチック特化 — なぜプロは組合せ馬券か (数学)

### 3.1 Benter の「乗法的アドバンテージ」数値例 (1994 論文)

単勝では両方とも -EV な 2 頭:
- Horse A: c=0.115, div=8.3 → **E=0.955** (負)
- Horse B: c=0.060, div=16.6 → **E=0.996** (負)

これを **馬連 (両順)** に組むと:
```
C_quinella = 0.115·0.060/(1-0.115) + 0.060·0.115/(1-0.060) = 0.0151
公衆暗黙   = 0.0108
E = 0.0151 × 76.85 ≈ 1.16  (+16%)
```
**単勝で負ける馬同士でも、組合せでは +16%**。Benter: *"the more exotic (i.e. specific) the bet, the higher the advantage."* Pick-6 のような ultra-exotic では「modest な予測力でも +EV が出る」。実運用でも「exotic pool の rate-of-return は simple pool より一般に高かった」。

### 3.2 なぜ公衆は組合せが下手か — misperceptions モデル (Snowberg & Wolfers 2010, NBER w15923)

- 査読研究が exacta/quinella/trifecta 60,288 件で検証: **「公衆が条件付き確率を *誤認* する misperceptions モデル」が「リスク愛好モデル」より説明力が高い** (misperceptions の係数 0.63, SE 0.014)。
- 意味: 公衆は単勝オッズから 2着・3着の条件付き確率を **正しく合成できない** (favorite-longshot bias を条件付き分布に二重適用する等)。**この合成の下手さ = 我々が正しい条件付き確率を出せれば overlay が残る場所**。

### 3.3 我々が「単勝のみ僅かに市場接近、3連単-EV」なのとプロの差

| | 公衆 | 我々のモデル | プロ (Benter) |
|---|---|---|---|
| win 1着率 | 効率的 | **市場に僅か接近** (β=0.78 で単勝 ROI 95.9%, 市場 88.5%) | 市場を上回る (combined R² +0.0178) |
| 2着・3着 **条件付き** 分布 | **下手** (misperceptions) | **素の Harville^λ + show_bias 近似** (ev.py:167-188) | **MLE 推定した σ,τ (γ=.81, δ=.65) で公衆バイアスを実証的に補正** |

**核心**: Benter の λ=0.81/0.65 は当 repo も `DEFAULT_LAMBDA_2/3` として採用済 (ev.py:35-36) だが、**Benter はこれを自分のデータで MLE 推定し、予測 vs 実測の Z 統計量がほぼ 0 になるまで calibrate した** (論文 Table 11/12)。我々は文献値をそのまま流用しているだけで **自前データで calibrate していない**。3連単 -EV の主因はここ: **2-3着条件付き確率が calibrate されていないので、組合せ確率 P(a,b,c)=P(a)·P(b|a)·P(c|a,b) の後ろ 2 項が市場とズレ、overlay 判定が当てにならない**。

---

## 4. Kelly / 分割Kelly / dutching / 小さな +EV のポートフォリオ

- **Benter は full Kelly を明確に警告**: アドバンテージを 2 倍に過大評価すると成長率が負に; full Kelly は >50% drawdown が常態。**"fractional Kelly (½ or ⅓) is advisable"**。
- **parimutuel 固有の上限**: 自分の賭けが配当を下げるので、無限 bankroll でも **profit が減少に転じる最大 bet サイズ**が存在 (例: c=0.06, div=20, pool=$100k → 最大 profitable bet=$416, その⅔の $277 で profit の 90% を低リスクで取れる)。
- **当 repo は既に joint Kelly + ½Kelly 併記 + トリガミ防止 margin=1.10 を実装済** (`portfolio.py`, CLAUDE.md)。これは Benter/Ziemba の処方とよく整合。**唯一の不足は ①pool-size-limited 制約 (自分の賭けが NAR 小プールのオッズを動かす分) が未モデル化 ②kelly_fraction が full (1.0) 既定** (実弾は ½ 推奨を frontend 表示のみ)。
- **Dutching**: 複数の +EV を同一 outcome に張り損益を平準化。**当 repo の joint Kelly はこれの上位互換** (相関・排他を考慮した成長率最適配分)。別途 dutching を足す必要はない。

---

## 5. CLV / 直前投票 / 締切直前の金の動き

- **Australian 14,854 races (2006 全シーズン) 研究**: **"late money is smart money"**。発走直前の金が ①subjective prob を true prob に近づけ ②favorite-longshot bias を縮小。→ **最終オッズが最も効率的**。
- Ziemba (2023): 現代 parimutuel は **「半分以上の金が発走中まで pool 未記録」**。CAW がアグリゲータ経由で締切間際に注入。
- **CLV の二面性 (当 repo にとって決定的)**:
  - 良い面: 当 repo の「締切1分前発火」(`bet_scheduler`, CLAUDE.md) は「最も効率的なオッズで判断」という意味で正しい。
  - **致命的な面**: **parimutuel では「賭けた瞬間のオッズ ≠ 確定オッズ」**。締切1分前に見た 11倍 が確定 5倍 になる (PTF が報告した T O Elvis: morning 30-1 → 11-1 表示 → off 5-1)。**当 repo のバックテストは `settled_odds.parquet` の確定オッズ (=払戻) で EV を測る**が、実戦は締切前オッズで賭ける。**この乖離を測っていない** = バックテスト ROI が実戦 ROI を系統的に過大評価している可能性。**最優先の検証項目** (§9)。

---

## 6. 複勝・ワイド等 place 系の過小評価狙い / 3着スペシャリスト

- **重要な訂正 (Benter 1994 脚注)**: *"a horse with a positive expected return in the win pool will have a LOWER expected return as a place or show bet, given that the public bets consistently in the different pools."* つまり **「単勝 +EV の馬は複勝では EV が下がる」のが公衆が pool 間で整合的に賭けている場合の通常**。素朴な「place は控除率低いから過小評価」は誤り。
- **Dr Z (Ziemba & Hausch 1987) の place/show overlay が成立したのは「pool 間の不整合」**: 公衆の win pool 暗黙率と、place/show pool での賭け額が **食い違う時だけ**。つまり「ある馬の win 人気 vs place/show 人気の乖離」を直接観測して張る。
- **当 repo は既にこの観測器を持っている**: `src/market_signal.py` が **単勝オッズと複勝オッズの implied prob 比 (place/win ratio)** を per-horse で計算し「3着型 (市場が top3 は堅いが1着無しと見る)」を検出 (market_signal.py:1-12, CLAUDE.md の market_signal 記述)。**ただし現状 EV 計算に組み込まず horse table の overlay 表示のみ**。→ §7 提案 3 で「これを Dr Z 式の place/show overlay 判定に昇格」する。
- **wide の魅力 (日本特有)**: 控除率 22.5% (馬単/3連複の 25%, 3連単 27.5% より低い) + 的中率が高い (3着以内2頭)。当 repo の settled データでも wide median 5.0倍 / 的中率高 → トリガミ防止 margin と相性が良い。CLAUDE.md も「keiba.go.jp/JRA 経路では wide/複勝が支配的」と観測済。

---

## 7. 日本特有 — JRA-VAN / AI予想 / NAR vs JRA の非効率

- **JRA-VAN データマイニング (DM)**: JRA 公式が AI で全レース事前予測を会員提供。**JRA-VAN/TARGET でファクター別回収率を機械的に集計**するのが日本の「データ派」の定石 (例: note の「6番人気の単勝回収率 81.7% が最高」「距離短縮馬は過小評価」)。これらは **単一ファクターの回収率 bin** で、§3 の misperceptions と同じく「公衆が特定状況を系統的に誤評価」を突く。**当 repo の LightGBM は本質的にこれの多変量版**。
- **AI予想 (umaro 等)**: 商用 AI 予想は多数あるが **+EV 実績の文献的裏付けは無い** (予想商材と同じく懐疑的に扱うべき)。
- **NAR vs JRA の非効率の所在**:
  - **NAR (地方)**: 小プール → 公衆バイアス大・効率低い *が* CLV が自分に不利 (賭けでオッズが動く)・pool manipulation リスク・データ品質低い。**当 repo が NAR 偏重なのは「非効率が大きい」点で理に適うが「最終オッズが読めない/動かす」点で実弾化リスクが高い**。
  - **JRA (中央)**: 大プール → 効率的だが安定。Benter 型「巨大 turnover を僅かなエッジで回す」が成立しうる唯一の場。**データ品質も高い (馬柱・上3F・通過順が揃う)**。
- **→ 戦略的含意**: モデルの edge 検証は **JRA で行う** (オッズが安定し最終≒締切前)。NAR は「非効率は大きいが実行リスクも大きい」ので、JRA で確立した手法を慎重に移植する。

---

## 8. 「どうせ -EV なら全力で3連単を当てにいってよいか?」— 数学的に正直な答え

**短答: 全力 -EV 3連単は、ただ速く負ける (分散が大きいので破産が早まるだけ)。**

- EV<1 の賭けは試行を増やすほど確実に資金 → 0 (大数の法則)。3連単は分散が極大 (settled mean 666倍, max 58万倍) なので、**「たまに大きく当たる」体験はあるが期待成長率は負、かつ drawdown が深く破産確率が高い**。「当てにいく」(prioritize="hit") はトリガミ防止で損失を限定するだけで **-EV を +EV にはしない** (portfolio.py:196 のコメントが既に正直に明記)。
- **ただし条件付きで YES の余地**: §3 の通り、**組合せプールは公衆の条件付き確率合成の下手さ (misperceptions) で +EV overlay が残りうる**。それが **いつ** 成立するか:
  > **我々の win モデルが見落とす「条件付き確率」を正しく出せる時**。具体的には:
  > (a) **2着・3着の条件付き分布を自前データで calibrate** し (現状は文献 λ 流用のみ)、
  > (b) 公衆の trifecta 暗黙率 (= `settled`/live odds から逆算) と我々の P(a,b,c) を比較して、
  > (c) **公衆が系統的に過小評価する条件付きパターン** (例: 「1着型の堅い軸 → 2着に3着スペシャリスト → 3着に人気薄」の鎖) を特定できた時。
- **つまり「全力で当てにいく」のではなく「公衆の条件付き合成エラーが大きい *特定の3連単*」だけを、calibrate 済の条件付き確率で狙い撃つ**のが唯一の数学的に正しい3連単 +EV 経路。これは「全力」とは逆の「極めて選択的」な戦略。

---

## 9. 実行提案 (優先順位つき, 上位3手法)

> 共通の検証基盤: 既存 `data/datasets/settled_odds.parquet` (7,166 race × 全7券種の確定オッズ) + `all.parquet` (finish_pos) + `scripts/sliding_window_eval.py` (独立 LGBM 再訓練) + `scripts/full_history_backtest.py`。**全提案を sliding-window (異なる LGBM・異なる valid) で検証し in-sample の罠を避ける**。

---

### 提案 1 ★最優先: Benter 二段の「正しい」実装 — β を別 partition で MLE 推定して凍結

**狙い**: 我々の market-as-feature が overfit になった根本原因 (β の in-sample sweep) を、Benter の手順 (fundamental 凍結 → 別データで α/β を 1 回 MLE) に置き換える。

**期待エッジ**: Benter combined の対公衆 ΔR²=+0.0178。現実 ROI で **市場 (β=1.0) 比 +1〜3pt** を out-of-sample で *安定的に* (= sweep peak でなく MLE 固定値で) 出せれば、§1.3 の「市場を 0.5-1.5% 上回る」天井に乗る。単勝で既に W3/W4 両方で +7-8pt 出ている (CLAUDE.md) のを、**MLE 固定 β で再現できるか**が試金石。

**実装スケッチ** (`src/train.py` + `src/ev.py`):
```python
# train.py: 時系列で 3 分割 (既存は 2 分割)
#   fold_A (古い 60%): fundamental LGBM を学習
#   fold_B (中 20%):   fold_A モデルで f_i を予測 → π_i と combine logit を MLE
#   fold_C (新 20%):   完全 hold-out で combined を評価 (β は触らない)
def fit_benter_beta(model_A, fold_B_df) -> float:
    """fold_B 上で α·log f + β·log π の conditional-logit log-likelihood を最大化。
    α=1-β 制約 (repo の現行と整合) で β を 1 次元最適化 (scipy.optimize.minimize_scalar)。
    f_i = softmax(model_A.predict / T), π_i = power_method_overround(1/win_odds)。
    返り値 β* を metadata['market_blend_mle'] に保存。"""
    from scipy.optimize import minimize_scalar
    def neg_ll(beta):
        ll = 0.0
        for rid, g in fold_B_df.groupby('race_id'):
            f = softmax(model_A.predict(g[feat]) / T)
            pi = power_method_overround({n:1/o for n,o in ...})
            logit = (1-beta)*np.log(f) + beta*np.log(pi)
            p = softmax(logit)
            ll += np.log(p[winner_idx])   # winner only
        return -ll
    return minimize_scalar(neg_ll, bounds=(0,1), method='bounded').x
# ev.py: estimate_probs は既に loglinear blend 実装済 (144-156)。
#   BLEND_DEFAULT を metadata['market_blend_mle'] から読むよう 1 行変更するだけ。
```
変更点は最小: **`estimate_probs` の数式は既に Benter と同一**。`train.py` に fold_B での β MLE を足し、`ev.py` が定数でなく metadata の `market_blend_mle` を読むようにするだけ。

**検証**: `sliding_window_eval.py` を 3-fold 化し、**MLE 固定 β** で W3/W4 両 window の単勝 ROI + 3連単 PL hit を測る。**固定 β が両 window で市場を上回れば本物、片方でも下回れば overfit**。実装コスト: **中** (train.py に ~40 行, ev.py に ~2 行, eval に ~30 行)。リスク: 低 (既存数式の再パラメータ化のみ)。

---

### 提案 2 ★高: 2-3着条件付き確率の自前 calibration (Henry/Benter σ,τ を MLE 推定) — 3連単 overlay の前提条件

**狙い**: §3.3 の核心。現状 `DEFAULT_LAMBDA_2=0.81 / LAMBDA_3=0.65` は **Benter の香港データ由来の文献値の流用**。これを **当 repo の `settled_odds`+`all.parquet` で MLE 推定**し、Benter Table 11/12 と同じ「予測 vs 実測の Z≈0」calibration を達成する。これが達成できて初めて P(a,b,c) の後ろ2項が信頼でき、3連単/3連複 overlay 判定が意味を持つ。

**期待エッジ**: 直接 ROI を生むのでなく「3連単 EV の校正」。calibrate 後、`build_bundle` の pool に乗る exotic 候補の +EV 判定が現実化する。**+EV overlay は misperceptions 研究 (Snowberg-Wolfers) が実在を保証**しているので、条件付きが正しければ取り出せる。

**実装スケッチ** (新規 `scripts/calibrate_place_lambda.py`):
```python
# all.parquet の finish_pos で「1着が a の時、2着が誰か」の実測条件付き頻度を作り、
# place2[i]=win[i]^λ2 / place3[i]=win[i]^λ3 の λ2,λ3 を MLE。
# Benter の bias 検証 (論文 Table 11/12) を再現: 予測 P(2着) の bin ごとに実測 hit 率を出し
# Z 統計量が 0 に近い λ を選ぶ。win[i] は提案1の combined 確率を使う。
def fit_lambdas(races) -> tuple[float,float]:
    def neg_ll(params):
        l2, l3 = params
        ll = 0.0
        for r in races:                       # r: win prob dict + actual (a,b,c)
            s2 = {k: r.win[k]**l2 for k in r.win}
            p2 = s2[r.b] / (sum(s2.values()) - s2[r.a])      # P(b=2nd | a=1st)
            s3 = {k: r.win[k]**l3 for k in r.win}
            p3 = s3[r.c] / (sum(s3.values()) - s3[r.a] - s3[r.b])
            ll += np.log(max(p2,1e-9)) + np.log(max(p3,1e-9))
        return -ll
    return minimize(neg_ll, x0=[0.81,0.65], bounds=[(0.3,1.2)]*2).x
# 出力を ev.py の DEFAULT_LAMBDA_2/3 (35-36) に反映 (or metadata)。
# show_bias (ev.py:175-185) の効きも同時に grid 検証。
```

**検証**: ①calibration (予測 2/3着 prob bin の Z 統計量, Benter Table 11/12 形式) ②`sliding_window_eval` の 3連単 PL hit-rate / mean-rank が λ 更新前後で改善するか ③`settled_odds` 全7券種で「我々の P × 確定オッズ」の平均が券種ごとに takeout 理論値 (単複 0.80, 3連単 0.725) にどれだけ近づくか (= calibration の客観指標)。実装コスト: **中〜大** (新規スクリプト ~120 行 + eval 連携)。リスク: 中 (λ が NAR ダート偏重データに過適合しないよう JRA/NAR 別々に推定推奨)。

---

### 提案 3 ★中: 「pool 間不整合 overlay」を market_signal から EV へ昇格 (Dr Z の日本版)

**狙い**: §6。当 repo は既に `market_signal.py` で単勝 vs 複勝の implied prob 比 (place/win ratio) を計算し「3着型」を検出済だが **horse table 表示のみで EV 未使用**。これを **Dr Z 式の「win pool 暗黙率 vs place/show pool 賭け額の不整合」検出に昇格**し、不整合が閾値超の馬を ①複勝/ワイドの直接 +EV 候補に ②3連単/3連複の2-3着スロットの優先軸に、使う。

**期待エッジ**: Dr Z place/show は文献的に +EV 実績がある (Ziemba & Hausch 1987, Breeders' Cup 実戦)。**ただし §6 の Benter 訂正の通り「pool 間整合時は単勝+EV 馬の複勝はむしろ -EV」**なので、素朴な複勝張りは禁物。**不整合 (place 人気 ≠ win 人気) を直接観測した時だけ**張るのが肝。日本は単勝・複勝とも控除率 20% で最良なので overlay が残りやすい。

**実装スケッチ** (`src/market_signal.py` 拡張 + `portfolio.py` 連携):
```python
# 現状 place_to_win_ratio (market_signal.py) を Dr Z の「期待値乖離」に変換:
#   win pool 暗黙率 q_win[i] から Harville で「複勝圏内」理論率 q_show_theory[i] を出し、
#   実際の複勝オッズ暗黙率 q_show_market[i] と比較。
#   q_show_market[i] / q_show_theory[i] >> 1 (市場が複勝で過小評価) → 複勝/ワイド overlay。
def drz_show_edge(rd) -> dict[int, float]:
    q_win = power_method_overround({h.number: 1/h.win_odds for h in horses})
    q_show_theory = {i: place_prob((i,), probs_from_win(q_win)) for i in ...}  # Harville
    q_show_market = {i: 1.0 / h.place_odds_min for h in horses}                # fuku 下限
    return {i: q_show_market[i] / q_show_theory[i] for i in ...}  # >1.0 = 市場過小評価=妙味
# build_bundle の候補生成時、drz_show_edge>閾値 の複勝/ワイドを優先プールに、
# かつ3連単の2-3着スロットを当該馬に寄せる (条件付き確率の軸固定)。
```

**検証**: `settled_odds` の `place`/`wide` 行で「drz_show_edge>1.1 の馬の複勝」の実 ROI を全 7,166 race で測る (これは settled が複勝・ワイドを全カバーするので **追加 scrape 不要で即測れる**)。ROI>100% かつ sliding-window で安定なら本物。実装コスト: **小〜中** (market_signal に ~30 行, build_bundle 連携 ~20 行, eval ~40 行)。リスク: 低 (既存 market_signal の自然な拡張、複勝・ワイドは of settled で完全検証可能)。

---

### (検証専用・提案ではないが最優先で実施すべき) CLV 乖離の測定

§5 の通り **バックテスト (確定オッズ) と実戦 (締切前オッズ) の乖離が ROI を過大評価している可能性**。当 repo は live 運用で `data/predictions/<rid>.json` snapshot に「賭けた時のオッズ」を保存しているはず。**snapshot のオッズ vs `settled_odds` の確定オッズを突き合わせ、券種別・JRA/NAR 別の系統ドリフトを測る**。NAR 小プールで「自分の見た 11倍 → 確定 5倍」が常態なら、**NAR の実弾は CLV 負けで -EV が深まる** → JRA 優先の判断材料になる。実装コスト: 小 (既存 snapshot と settled を join するだけ)。**これをやらずに上記 ROI を信じるのは危険**。

---

## 10. 出典一覧

- **Benter, W. (1994)** "Computer Based Horse Race Handicapping and Wagering Systems: A Report." gwern.net/doc/statistics/decision/1994-benter.pdf, datagolf.com mirror. (二段 logit, λ=.81/.65, 馬連+16%例, fractional Kelly, exotic advantage)
- **Acta Machina** "Revisiting the Algorithm that Changed Horse Race Betting" (annotated Benter). actamachina.com/posts/annotated-benter-paper (R² 0.1218/0.1245/0.1396, ΔR² 対照実験, ROI 0.25-1.5%)
- **Ziemba, W.T. (2023)** "Pari-Mutuel Betting Markets: Racetracks and Lotteries Revisited." LSE eprints 120846. (シンジケート ~$1B, リベート, "半分以上の金が発走中まで未記録", Benter への Dr Z 助言)
- **Hausch, Lo & Ziemba (1994/2008)** "Efficiency of Racetrack Betting Markets." World Scientific. (Dr Z place/show, exotic 非効率, Ch37-45)
- **Snowberg, E. & Wolfers, J. (2010)** "Explaining the Favorite-Longshot Bias: Is it Risk-Love or Misperceptions?" NBER w15923. (exotic は misperceptions モデルが優位, n=60,288)
- **Late Money study (ECU 2013)** "Late Money and Betting Market Efficiency: Evidence from Australia." (14,854 races, "late money is smart money")
- **CAW/リベート**: Paulick Report, Thoroughbred Idea Foundation (Takeout 201), HorseRaceInsider, r/algobetting (リベート無しでは net loser)
- **Thoroughbred Idea Foundation** "Answering Your Questions About Pool Manipulation" (NAR 小プール manipulation)
- **PTF / Marshall Gramm** (CAW late money, T O Elvis 30-1→5-1 の CLV 実例)
- **日本控除率**: JRA「馬券のルール」, netkeiba コラム cid=27253, JRA-VAN, 競馬ナビ/競馬ディスカバリー (券種別控除率)
- **日本データ派**: JRA-VAN データマイニング, note (人気順回収率, 距離短縮過小評価)

---

## 10. 検証結果 (提案3 Dr Z + CLV を実装・計測した結論, 2026-06-01)

`scripts/clv_drz_analysis.py` でライブ snapshot (bet時オッズ) + 結果 168 race を計測。

### CLV (締切→確定ドリフト) — 重要
3連単の bet時オッズ → 確定払戻 ドリフト = **平均 −10.4% / 中央 −9.2% (95%CI[−14.3,−6.3])**。
確定払戻が bet時オッズを上回るのは 39% だけ。= **締切~1分前に見えるオッズより確定払戻は
約10%低い** (late money が人気サイドに入り配当が縮む / 小プールの NAR で顕著)。

含意:
- settled-odds バックテストは **確定オッズで払戻**しているので payout は正しい (楽観でない)。
- ただし **bet時オッズで P×O を評価すると 3連単 EV を ~10% 過大評価**する。production の
  `recommended_bundle` は bet時オッズで選抜するため、3連単脚の実 EV は ~10% 低い。
- `portfolio.TORIGAMI_MARGIN = 1.10` が **この −10% をちょうど吸収**する (payout ≥ 1.10×stake を
  bet時オッズで要求 = 確定で ~1.0 に縮んでもトリガミにならない)。既存 margin は妥当と検証された。

### Dr Z place/wide overlay — 配線しない (実測 -EV)
snapshot の `bet_tables['place'/'wide']` の px_o≥1.02 (モデルの place/wide +EV picks) を
実際に買った時の ROI:
- place: **48.9%** (95%CI[25.5,77.8], hit 27/206) = 大幅 -EV。
- wide:  **68.5%** (95%CI[39.4,105.6], hit 46/1862) = -EV (CI 下限 39%)。
しかも payout に bet時オッズを使った楽観値なので、CLV −10% を入れると更に悪い。
→ **build_bundle への配線は見送り** (損をする)。研究の警告「win モデルの place EV は
プール整合時は通常 -EV、overlay は pool 不整合からのみ」が実データで確認された。
真の Dr Z は「win プールと place プールの**不整合**」を突くものでモデルの place EV ではない。

### 結論
両提案とも配線基準 (ROI 95%CI 下限 > 100%) を満たさず **配線せず**。計測で「やらない方が良い」
ことが分かったのが成果。唯一 actionable な発見は CLV −10% で、これは既存 margin=1.10 が
吸収済 = production は既に妥当。

---

## 11. 提案1 (track-variant + pace 図表) を実装・検証した結論 (2026-06-01)

「市場より当たる win 確率」を作るため、実データ駆動の speed 図表 v2 を実装して β-MLE で検証:
- `build_par_table.py`: 全 past_runs (19,440 ユニーク過去 race) から per-condition の par
  勝ち時計を実測 (ハードコード par を置換)。
- `build_v2_features.py`: speed_v2 (par からの走破タイム差) + pace_v2 (上がり3F vs par_last3f
  の終い脚) + trip (通過順の前後/位置取り変化) を全 (race,horse) で算出。
- `test_speed_v2.py`: baseline (既存特徴量) vs v2 (既存+v2) で 3-fold β-MLE。

結果 (holdout C, N=1434/1152):
| seg | baseline β | v2 β | v2 holdout 単勝 | 市場 | log-loss |
|---|---|---|---|---|---|
| ALL | 1.000 | **1.000** | 81.5% | 81.3% | 1.587 (同一) |
| NAR | 1.000 | 0.979 | 80.9% (<baseline 81.4) | 81.2% | 1.596 (同一) |

**結論: v2 図表は市場を上回らない (本物の edge ではない)。** β は 1.0 に張り付いたまま (NAR の
0.979 は OOS ROI が baseline より悪化・log-loss 同一 = MLE 誤差で実質無効)。par/pace/trip も
**既に市場に織り込まれている**。我々の情報集合 ⊂ 市場の情報集合、が再確認された。

含意: **公開データを上手く処理しても、この市場 (JRA/NAR とも) は上回れない。** 本物の edge は
(a) 真に直交する情報 = 直前/軟情報 (Claude alerts に振った — 要 OOS 検証) か (b) 我々が持たない
専有データ (区間タイム全通過点・GPS・厩舎私的情報) か (c) リベート (日本に無い) からしか出ない。
v2 は production に統合しない (改善にならずノイズ/複雑さを足すだけ)。計測で「やっても無駄」と
分かったのが成果 = -EV を確定的に避けられる。
