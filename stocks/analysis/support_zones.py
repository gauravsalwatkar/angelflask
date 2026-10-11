import numpy as np
from scipy.signal import argrelextrema
from sklearn.cluster import DBSCAN


def _finite(value):
    return float(np.nan_to_num(value, nan=0.0, posinf=0.0, neginf=0.0))


def find_strong_support_zones(df, swing_order=5, eps_pct=0.003, lookahead=10):
    """Cluster swing lows into support zones, strongest first.

    `df` needs date, low, high and volume columns.
    """
    df = df.sort_values("date").reset_index(drop=True)

    swing_idx = argrelextrema(df["low"].values, np.less, order=swing_order)[0]
    swing_lows = df.iloc[swing_idx].copy()
    if swing_lows.empty:
        return []

    prices = swing_lows["low"].values.reshape(-1, 1)
    eps = float(prices.mean() * eps_pct)

    swing_lows["cluster"] = DBSCAN(eps=eps, min_samples=2).fit_predict(prices)
    swing_lows = swing_lows[swing_lows["cluster"] != -1]

    df_volume_mean = df["volume"].mean()
    if not np.isfinite(df_volume_mean) or df_volume_mean == 0:
        df_volume_mean = 1  # avoid division by zero

    zones = []
    for cluster_id in swing_lows["cluster"].unique():
        zone = swing_lows[swing_lows["cluster"] == cluster_id]
        level = float(np.nanmean(zone["low"]))
        touches = int(len(zone))

        bounce_strength = []
        volume_factor = []
        for idx in zone.index:
            future = df.iloc[idx : idx + lookahead]
            if not future.empty:
                bounce_strength.append(_finite((future["high"].max() - level) / level))
                volume_factor.append(_finite(future["volume"].mean() / df_volume_mean))

        avg_bounce = _finite(np.nanmean(bounce_strength))
        avg_volume = _finite(np.nanmean(volume_factor))
        strength_score = float(touches * 1.5 + avg_bounce * 100 + avg_volume)

        zones.append({
            "support_level": round(level, 4),
            "zone_low": round(float(np.nanmin(zone["low"])), 4),
            "zone_high": round(float(np.nanmax(zone["low"])), 4),
            "touches": touches,
            "avg_bounce_pct": round(avg_bounce * 100, 2),
            "volume_factor": round(avg_volume, 2),
            "strength_score": round(strength_score, 2),
        })

    return sorted(zones, key=lambda z: z["strength_score"], reverse=True)
