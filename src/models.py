"""ドメインモデル (競馬 / horse racing 用)。

KEIRIN 版との主な違い:
  - Player → Horse (車番 → 馬番)
  - 脚質 (逃/両/追/差) は競馬でも「脚質」と呼ぶが、概念が異なる
    → 逃げ / 先行 / 差し / 追い込み の 4 区分
  - Line / LinePower は存在しない (競馬にラインの概念はない)
  - 枠番 (1-8) と馬番 (1-18) は別 (枠は馬番複数を束ねる)
  - 騎手 / 斤量 / 馬体重 / 単勝オッズ といった競馬固有のフィールドを追加
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Horse:
    """1 頭の出走馬。

    rate 系は累計 (1 着率 / 連対率 / 3 連対率) と解釈する。netkeiba の馬情報
    から取れる値をそのまま乗せる。取得できないフィールドは 0 / "" のデフォルト。
    """
    number: int                # 馬番 (1..N)
    name: str                  # 馬名
    bracket: int = 0           # 枠番 (1..8)
    sex_age: str = ""          # 性齢 (例: "牡4", "牝3", "セ5")
    weight_kg: float = 0.0     # 斤量 (kg)
    body_weight: int = 0       # 馬体重 (kg)。0 で不明
    body_weight_diff: int = 0  # 前走比 (kg)
    jockey_name: str = ""      # 騎手名
    jockey_id: str = ""        # 騎手 ID (netkeiba 内部)
    trainer_name: str = ""     # 調教師名
    rating: float = 0.0        # レーティング相当 (netkeiba の指数 / 競走得点相当)
    win_rate: float = 0.0      # 1 着率 %
    quinella_rate: float = 0.0 # 連対率 %
    trio_rate: float = 0.0     # 3 連対率 %
    style: str = ""            # 脚質 (逃/先/差/追) — 取れれば
    win_odds: float = 0.0      # 単勝オッズ (取れれば。market_blend で使う)
    absent: bool = False       # 取消 / 除外
    horse_id: str = ""         # netkeiba 内部 ID
    interview_comment: str = ""  # コメント (調教師談話など)
    past_runs: list["PastRun"] = field(default_factory=list)  # 馬柱から (直近 5 走)

    @property
    def pure_second(self) -> float:
        """純 2 着率 = 連対率 − 1 着率。"""
        return max(self.quinella_rate - self.win_rate, 0.0)

    @property
    def pure_third(self) -> float:
        """純 3 着率 = 3 連対率 − 連対率。"""
        return max(self.trio_rate - self.quinella_rate, 0.0)


@dataclass
class Weather:
    """発走時刻ターゲットの天候。"""
    code: int = 0                # 天候コード (晴/曇/雨/雪)
    temperature: float = 0.0     # 気温 ℃
    wind_speed: float = 0.0      # 風速 m/s
    wind_direction: int = 0      # 風向コード
    precipitation: float = 0.0   # 降水量 mm/h
    track_condition: str = ""    # 馬場状態 (良/稍重/重/不良)
    target_at: int = 0           # 予報対象 unix


@dataclass
class Prediction:
    """netkeiba の予想 (AI / 専門家)。"""
    name: str
    is_ai: bool
    comment: str
    winning_rate: int            # 累計勝率 %
    winning: int
    total: int
    trifecta_keys: list[tuple[int, int, int]]


@dataclass
class PastRun:
    """馬の過去 1 走分。馬柱 (shutuba_past.html) からパース。

    フィールド命名は西田式スピード指数等の計算で直接使えるよう、SI 単位 / 標準値で持つ。
    finish_pos が None の場合: 4 着以下 (馬柱に着順が明示されていない)。
    own_time_sec は「自分の走破時刻」。Data07 の winner_time_diff (正なら遅れ) を加味して算出。
    """
    date: str = ""               # "2026.04.11" 形式 (元データママ)
    venue: str = ""              # 場名
    race_no: int = 0             # R 数
    race_class: str = ""         # 例 "4歳以上2勝クラス" "G1" "B1B2" etc.
    race_id: str = ""            # netkeiba 12桁 race_id (リンクから抽出)
    surface: str = ""            # "芝" / "ダート" / "障害"
    distance: int = 0            # m
    going: str = ""              # 馬場 "良" / "稍" / "重" / "不"
    winner_time_sec: float = 0.0 # Data05 の勝ち馬タイム (秒換算)
    time_diff_sec: float = 0.0   # Data07 の括弧内。winner との時間差 (秒、+=遅れ、-=リード)
    field_size: int = 0          # 出走頭数
    horse_number: int = 0        # 当該馬の馬番
    popularity: int = 0          # 人気
    jockey: str = ""             # 騎手名
    weight_kg: float = 0.0       # 斤量
    passing: str = ""            # 通過順 "12-14" or "3-3-1-1" など (文字列のまま)
    last_3f_sec: float = 0.0     # 上がり 3F (秒)
    body_weight: int = 0         # 馬体重 (kg)、0=計不
    body_weight_diff: int = 0    # 前走比 (kg)
    finish_pos: Optional[int] = None  # 1/2/3 のみ。それ以外は None (馬柱は明示しないため)

    @property
    def own_time_sec(self) -> float:
        """自分の走破タイム (秒)。馬柱は勝ち馬タイム + 時間差で表記されているため再構成。"""
        if self.winner_time_sec <= 0:
            return 0.0
        return self.winner_time_sec + self.time_diff_sec

    @property
    def won(self) -> bool:
        return self.finish_pos == 1

    @property
    def placed(self) -> bool:
        return self.finish_pos in (1, 2)

    @property
    def showed(self) -> bool:
        return self.finish_pos in (1, 2, 3)


@dataclass
class Race:
    cup_id: str                  # 開催 ID (netkeiba 内部)
    schedule_index: int          # 開催何日目
    race_number: int             # R 数 (1..12)
    venue_id: int                # 競馬場 ID
    venue_name: str              # 競馬場名 (東京 / 阪神 / 中山 / ...)
    race_class: str              # クラス (G1 / G2 / G3 / OP / 3勝クラス / ...)
    distance: int                # 距離 m
    surface: str = ""            # 馬場 (芝 / ダート / 障害)
    direction: str = ""          # 周回方向 (右 / 左)
    weather_text: str = ""       # 当日の天候/馬場 (例: "晴 / 良")
    start_at: int = 0            # 発走 unix
    close_at: int = 0            # 締切 unix
    entries_number: int = 0
    horses: list[Horse] = field(default_factory=list)
    odds_updated_at: int = 0
    weather: Optional[Weather] = None
    predictions: list[Prediction] = field(default_factory=list)


@dataclass
class TrifectaOdds:
    """3 連単オッズ。key は (1着, 2着, 3着) の馬番タプル。"""
    key: tuple[int, int, int]
    odds: float
    popularity: int              # 人気順 (1 が最も売れている)
    absent: bool = False

    @property
    def label(self) -> str:
        a, b, c = self.key
        return f"{a}-{b}-{c}"


@dataclass
class BetOdds:
    """汎用 bet オッズ。bet_type と key 長で識別。

    bet_type:
      "quinella" 馬連 (key 長 2 / 順不同)
      "wide"     ワイド (key 長 2 / 順不同, 両馬3着以内)
      "exacta"   馬単 (key 長 2 / 順あり)
      "trio"     3連複 (key 長 3 / 順不同)
      "win"      単勝 (key 長 1)
      "place"    複勝 (key 長 1; odds は min-max のうち下限)
    """
    bet_type: str
    key: tuple[int, ...]
    odds: float
    popularity: int = 0
    absent: bool = False

    @property
    def label(self) -> str:
        return "-".join(str(k) for k in self.key)


@dataclass
class RaceData:
    race: Race
    trifecta: list[TrifectaOdds]
    # 馬連・ワイド・馬単・3連複・単勝・複勝 等の他 bet type オッズ。
    # キーは bet_type 文字列 ("quinella" / "wide" / "exacta" / "trio" / "win" / "place")。
    # parse 時点で fetch されなければ空 dict のまま (後方互換)。
    other_bets: dict[str, list["BetOdds"]] = field(default_factory=dict)


@dataclass
class Probabilities:
    """確率推定値。"""
    win: dict[int, float]        # 1 着確率 (合計 1.0)
    place2: dict[int, float]     # 2 着寄与 (絶対値のまま、PL 連鎖で正規化)
    place3: dict[int, float]     # 3 着寄与 (絶対値のまま、PL 連鎖で正規化)


@dataclass
class EvRow:
    key: tuple[int, int, int]
    odds: float
    popularity: int
    prob: float                  # 推定 3 連単的中率
    px_o: float                  # 期待回収率 (P × O)
    tier: str                    # honsen / chuana / oana / minus

    @property
    def label(self) -> str:
        a, b, c = self.key
        return f"{a}-{b}-{c}"


@dataclass
class BetEvRow:
    """汎用 bet type の EV row。bet_type と可変長 key で識別。"""
    bet_type: str
    key: tuple[int, ...]
    odds: float
    popularity: int
    prob: float
    px_o: float
    tier: str

    @property
    def label(self) -> str:
        return "-".join(str(k) for k in self.key)
