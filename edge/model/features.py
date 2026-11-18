# -*- coding: utf-8 -*-
"""
Cleaning + time-series features. Training and real-time inference use the same code.

Design
  * A per-node FeatureExtractor updates the features one row at a time.
    - Batch (build_features): stored time series are fed row by row in time order.
    - Real time (edge/edge_ai_ae.py via inference.StreamingClassifier): rows are fed as they arrive.
    Both paths use the same class, so the feature computation cannot diverge between them.
  * Time windows are based on timestamps, not row counts, so a lost packet does not shorten a window.
  * The 15 model inputs are listed in FEATURES below (same order as edge/model/model_meta.json).

의존: numpy, pandas
"""
import json
import math
import os
from collections import deque

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ constants (match the training data)
INTERVAL_MIN = 15.0
INTERVAL_H = INTERVAL_MIN / 60.0
CAP_H = 48.0                      # hours_since_wet, hours_below_zero, zero_run_h 상한
SENT_AIR, SENT_HUM, SENT_ROAD = -99.9, 99.9, -999.0
GLITCH_HZ = 5000.0
POR_ROAD = 85.0                   # DS18B20 전원투입 기본값
MAGNUS_B, MAGNUS_C = 17.625, 243.04
FFILL_LIMIT = 2                   # 순간값 전진채움 최대 샘플 수
FILL_VALUE = -999.0               # 모델 입력에서 결측 대체값 (트리가 별도 가지로 분리)

INSTANT_FEATURES = ["freq_norm", "road_temp", "air_temp", "air_hum"]
TEMPORAL_FEATURES = ["freq_norm_med_1h", "freq_norm_std_1h", "zero_frac_6h", "zero_run_h",
                     "hours_since_wet", "road_temp_slope_1h", "hours_below_zero", "dewpoint_margin"]
FLAG_FEATURES = ["freq_missing", "road_missing", "dht_missing"]
FEATURES = INSTANT_FEATURES + TEMPORAL_FEATURES + FLAG_FEATURES          # all 15 model inputs, in model order
CLASS_NAMES = ["Dry", "Wet", "Frozen"]


def load_node_config(path=None):
    """Per-node dry-surface baseline f_dry, measured once on a dry surface at installation. {node_id(int): f_dry_hz}"""
    path = path or os.path.join(HERE, "node_config.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return {int(k): float(v["f_dry_ref_hz"]) for k, v in cfg["nodes"].items()}


# ------------------------------------------------------------------ 정제
def clean_values(freq, air_temp, air_hum, road_temp):
    """센서 오류 -> NaN. 반환 (freq, air, hum, road)"""
    f = float(freq) if freq is not None else math.nan
    a = float(air_temp) if air_temp is not None else math.nan
    h = float(air_hum) if air_hum is not None else math.nan
    r = float(road_temp) if road_temp is not None else math.nan
    if not (f >= 0) or f > GLITCH_HZ:                 # 음수/NaN/글리치
        f = math.nan
    if a == SENT_AIR or h == SENT_HUM:                  # DHT 에러 센티넬: 둘 다 무효
        a, h = math.nan, math.nan
    if not (0 <= h <= 100):                             # 습도 > 100% 글리치
        h = math.nan
    if r == SENT_ROAD or r == POR_ROAD or not (-55 <= r <= 60):
        r = math.nan
    return f, a, h, r


def dewpoint(t, rh):
    if math.isnan(t) or math.isnan(rh) or rh <= 0:
        return math.nan
    g = math.log(rh / 100.0) + MAGNUS_B * t / (MAGNUS_C + t)
    return MAGNUS_C * g / (MAGNUS_B - g)


# ------------------------------------------------------------------ 행 단위 특징 추출기
class FeatureExtractor:
    """노드 1개 전용. update() 에 시간순으로 한 행씩 넣으면 그 시점의 특징 dict 를 돌려준다."""

    def __init__(self, f_dry_ref):
        self.f_dry = float(f_dry_ref)
        self.win = deque()                       # (t_h, freq_clean, road_clean) 최근 6h
        self.last_valid = {}                     # 전진채움용
        self.ffill_n = {}
        self.zero_run_start = None
        self.zero_run_count = 0
        self.last_wet_t = None
        self.sub_start = None
        self.last_t = None

    def _ffill(self, key, v):
        if not math.isnan(v):
            self.last_valid[key] = v
            self.ffill_n[key] = 0
            return v
        if key in self.last_valid and self.ffill_n.get(key, 0) < FFILL_LIMIT:
            self.ffill_n[key] += 1
            return self.last_valid[key]
        return math.nan

    def update(self, ts, freq, air_temp, air_hum, road_temp):
        t = pd.Timestamp(ts).value / 3.6e12                       # 시간(h) 단위 실수
        if self.last_t is not None and t <= self.last_t:
            raise ValueError("FeatureExtractor: 타임스탬프는 증가해야 함 (노드별 시간순 입력)")
        self.last_t = t
        fc, ac, hc, rc = clean_values(freq, air_temp, air_hum, road_temp)
        ff, af, hf, rf = (self._ffill("f", fc), self._ffill("a", ac), self._ffill("h", hc), self._ffill("r", rc))

        # 6h 창 유지 (24샘플: 지연 0~23 x 15분, 지터 여유로 23.5칸)
        self.win.append((t, fc, rc))
        lo6 = t - 23.5 * INTERVAL_H
        while self.win and self.win[0][0] < lo6:
            self.win.popleft()
        lo1 = t - 3.5 * INTERVAL_H                                 # 1h 창 = 지연 0~3 (4샘플)
        w1 = [(tt, f, r) for (tt, f, r) in self.win if tt >= lo1]
        f1 = [f / self.f_dry for (_, f, _) in w1 if not math.isnan(f)]
        r1 = [(tt, r) for (tt, _, r) in w1 if not math.isnan(r)]
        f6 = [f for (_, f, _) in self.win if not math.isnan(f)]

        med1 = float(np.median(f1)) if len(f1) >= 2 else math.nan
        std1 = float(np.std(f1)) if len(f1) >= 2 else math.nan
        zf6 = float(np.mean([f == 0 for f in f6])) if len(f6) >= 12 else math.nan
        if len(r1) >= 2 and (r1[-1][0] - r1[0][0]) > 0:
            tt = np.array([a for a, _ in r1]); rr = np.array([b for _, b in r1])
            slope = float(np.polyfit(tt - tt[0], rr, 1)[0])            # 도C/h
        else:
            slope = math.nan

        # 0Hz 연속 구간 / 최근 젖음
        if math.isnan(ff):
            zrun = math.nan
        elif ff == 0:
            if self.zero_run_start is None:
                self.zero_run_start, self.zero_run_count = t, 0
            self.zero_run_count += 1
            zrun = min(t - self.zero_run_start + INTERVAL_H, CAP_H)
            if self.zero_run_count >= 2:                            # 2샘플 이상 연속 0Hz = 젖음
                self.last_wet_t = t
        else:
            self.zero_run_start, self.zero_run_count = None, 0
            zrun = 0.0
        if self.zero_run_count >= 2:
            hsw = 0.0
        elif self.last_wet_t is None:
            hsw = CAP_H
        else:
            hsw = min(t - self.last_wet_t, CAP_H)

        # 노면 연속 영하 지속시간
        if math.isnan(rf):
            hbz = math.nan
        elif rf <= 0:
            if self.sub_start is None:
                self.sub_start = t
            hbz = min(t - self.sub_start + INTERVAL_H, CAP_H)
        else:
            self.sub_start = None
            hbz = 0.0

        td = dewpoint(af, hf)
        return {
            "freq_norm": ff / self.f_dry if not math.isnan(ff) else math.nan,
            "road_temp": rf, "air_temp": af, "air_hum": hf,
            "freq_norm_med_1h": med1, "freq_norm_std_1h": std1, "zero_frac_6h": zf6,
            "zero_run_h": zrun, "hours_since_wet": hsw, "road_temp_slope_1h": slope,
            "hours_below_zero": hbz,
            "dewpoint_margin": (rf - td) if not (math.isnan(rf) or math.isnan(td)) else math.nan,
            "freq_missing": float(math.isnan(ff)), "road_missing": float(math.isnan(rf)),
            "dht_missing": float(math.isnan(af) or math.isnan(hf)),
        }


# ------------------------------------------------------------------ 일괄(학습용)
def build_features(df, node_cfg):
    """df: ts,node_id,freq,air_temp,air_hum,road_temp 를 가진 DataFrame -> 같은 index 의 특징 DataFrame"""
    out = {}
    for node, g in df.groupby("node_id", sort=True):
        ex = FeatureExtractor(node_cfg[int(node)])
        g = g.sort_values("ts", kind="stable")
        for idx, r in zip(g.index, g[["ts", "freq", "air_temp", "air_hum", "road_temp"]].itertuples(index=False)):
            out[idx] = ex.update(*r)
    return pd.DataFrame.from_dict(out, orient="index").loc[df.index, FEATURES]


def to_matrix(feat, cols=FEATURES):
    return feat[cols].astype(float).fillna(FILL_VALUE).values


# ------------------------------------------------------------------ safety post-processing
SAFETY_MAX_PROB = 0.6
SAFETY_FROZEN_PROB = 0.25


def apply_safety(proba, max_prob=SAFETY_MAX_PROB, frozen_prob=SAFETY_FROZEN_PROB):
    """proba: (n,3) [Dry,Wet,Frozen]. 애매하고(최대확률<0.6) 결빙 가능성(>0.25)이 있으면 Frozen 으로 올림.
    반환: (pred, confidence, safety_applied)"""
    proba = np.atleast_2d(proba)
    pred = proba.argmax(1); conf = proba.max(1)
    up = (conf < max_prob) & (proba[:, 2] > frozen_prob) & (pred != 2)
    pred = np.where(up, 2, pred)
    conf = np.where(up, proba[:, 2], conf)
    return pred, conf, up


# ------------------------------------------------------------------ Wet confirmation filter
class WetConfirmFilter:
    """표시 상태가 Dry 일 때 Wet 예측이 '2샘플 연속'이어야 Wet 으로 바꾼다.
    가짜 0Hz 한 샘플은 진짜 젖음의 첫 샘플과 구분할 수 없으므로 다음 샘플로 확인한다.
    Frozen 예측은 절대 보류하지 않는다(안전 우선). 대가: Dry->Wet 전환 표시가 1샘플(15분) 늦어짐."""

    def __init__(self):
        self.display = None
        self.prev_pred = None

    def update(self, pred):
        pred = int(pred)
        held = pred == 1 and self.display == 0 and self.prev_pred != 1
        out = 0 if held else pred
        self.prev_pred, self.display = pred, out
        return out, held


def apply_wet_confirm(pred, node_ids):
    """일괄용: 행이 노드별 시간순으로 정렬돼 있다고 가정하고 노드마다 필터를 적용"""
    out = np.empty(len(pred), dtype=int); held = np.zeros(len(pred), dtype=bool)
    filters = {}
    for i, (p, n) in enumerate(zip(pred, node_ids)):
        f = filters.setdefault(int(n), WetConfirmFilter())
        out[i], held[i] = f.update(p)
    return out, held
