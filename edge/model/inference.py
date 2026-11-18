# -*- coding: utf-8 -*-
"""
Real-time inference wrapper, used by edge/edge_ai_ae.py and scripts/e2e_check.py.

  clf = StreamingClassifier(model_path="cloud/model_store/rf-v040.joblib")  # + model_meta.json + node_config.json
  res = clf.update({"ts":..., "node_id":1, "freq":..., "air_temp":..., "air_hum":..., "road_temp":...})
  -> {"state": final state, "confidence", "proba", "raw_state": the model's own prediction, "safety_applied",
      "unconfirmed_state": state before the Wet filter, "held_by_wet_confirm": Wet waiting for confirmation,
      "features"}
  Order: features -> model probabilities -> safety rule (unsure -> Frozen) -> Wet confirmation (Dry->Wet needs 2 rows)

Keeps one FeatureExtractor per node in memory (history needed: a 6 h window + a few state variables).
"""
import json
import os

import joblib
import numpy as np
import pandas as pd

from features import (FeatureExtractor, WetConfirmFilter, load_node_config, apply_safety, FEATURES, FILL_VALUE,
                      CLASS_NAMES)

HERE = os.path.dirname(os.path.abspath(__file__))


class StreamingClassifier:
    def __init__(self, model_path=None, meta_path=None, node_config_path=None):
        self.model = joblib.load(model_path or os.path.join(HERE, "..", "..", "cloud", "model_store", "rf-v040.joblib"))
        with open(meta_path or os.path.join(HERE, "model_meta.json"), encoding="utf-8") as f:
            self.meta = json.load(f)
        if self.meta["features"] != FEATURES:
            raise RuntimeError("feature order in model_meta.json differs from features.py - retrain the model")
        self.node_cfg = load_node_config(node_config_path)
        self.extractors = {}
        self.filters = {}

    def _extractor(self, node_id):
        node_id = int(node_id)
        if node_id not in self.extractors:
            if node_id not in self.node_cfg:
                raise KeyError(f"node{node_id} 의 f_dry 가 node_config.json 에 없음 (설치 보정 필요)")
            self.extractors[node_id] = FeatureExtractor(self.node_cfg[node_id])
        return self.extractors[node_id]

    def update(self, row):
        feat = self._extractor(row["node_id"]).update(
            row["ts"], row.get("freq"), row.get("air_temp"), row.get("air_hum"), row.get("road_temp"))
        x = np.array([[feat[c] for c in FEATURES]], dtype=float)
        x = np.where(np.isnan(x), FILL_VALUE, x)
        proba = self.model.predict_proba(x)[0]
        raw = int(proba.argmax())
        pred, conf, up = apply_safety(proba[None, :],
                                      self.meta["safety"]["max_prob"], self.meta["safety"]["frozen_prob"])
        flt = self.filters.setdefault(int(row["node_id"]), WetConfirmFilter())
        shown, held = flt.update(int(pred[0]))
        c = float(proba[shown]) if held else float(conf[0])     # 보류 시 표시 상태(Dry)의 확률을 신뢰도로
        return {"state": CLASS_NAMES[shown], "confidence": round(c, 4),
                "proba": {c: round(float(p), 4) for c, p in zip(CLASS_NAMES, proba)},
                "raw_state": CLASS_NAMES[raw], "safety_applied": bool(up[0]),
                "unconfirmed_state": CLASS_NAMES[int(pred[0])], "held_by_wet_confirm": bool(held),
                "features": feat}
