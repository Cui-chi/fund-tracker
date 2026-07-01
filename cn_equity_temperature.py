#!/usr/bin/env python3
"""Price-only China equity temperature model (CN_EQUITY_PRICE_TEMP_V1)."""

import datetime as dt
import math


FORMULA_VERSION = "CN_EQUITY_PRICE_TEMP_V1"
LIVE_SCORING_ENABLED = True
A500_CODE = "000510"
HS300_CODE = "000300"
A500_LAUNCH_DATE = dt.date(2024, 9, 23)


def _linear(value, points):
    if value <= points[0][0]:
        return float(points[0][1])
    if value >= points[-1][0]:
        return float(points[-1][1])
    for left, right in zip(points, points[1:]):
        if left[0] <= value <= right[0]:
            ratio = (value - left[0]) / (right[0] - left[0])
            return left[1] + ratio * (right[1] - left[1])
    raise ValueError("value outside interpolation range")


def moving_average_score(distance):
    return _linear(distance, [
        (-0.30, 90), (-0.20, 80), (-0.10, 65), (0.00, 50),
        (0.05, 35), (0.15, 15), (0.25, 0),
    ])


def drawdown_score(drawdown):
    return _linear(drawdown, [
        (-0.40, 90), (-0.30, 90), (-0.20, 75), (-0.15, 60),
        (-0.10, 45), (-0.05, 25), (0.00, 10),
    ])


def volatility_penalty(volatility):
    return _linear(volatility, [
        (0.15, 0), (0.20, 3), (0.25, 7), (0.30, 12),
        (0.35, 18), (0.45, 25),
    ])


def _clean_records(records):
    by_date = {}
    warnings = []
    rejected = 0
    for row in records or []:
        try:
            date = dt.date.fromisoformat(str(row.get("tradeDate"))[:10])
            close = float(row.get("close"))
            if not math.isfinite(close) or close <= 0:
                raise ValueError("close must be positive")
        except (TypeError, ValueError):
            rejected += 1
            continue
        if date in by_date:
            warnings.append("DUPLICATE_DATE_REMOVED:%s" % date.isoformat())
        by_date[date] = close
    rows = sorted(by_date.items())
    if rejected:
        warnings.append("INVALID_CLOSE_REJECTED:%d" % rejected)
    extreme = [
        rows[i][0].isoformat() for i in range(1, len(rows))
        if abs(rows[i][1] / rows[i - 1][1] - 1) > 0.50
    ]
    if extreme:
        warnings.append("EXTREME_DAILY_MOVE:%s" % ",".join(extreme[:5]))
    return rows, warnings, bool(extreme)


def _freshness(latest, as_of):
    lag = (as_of - latest).days
    if lag <= 3:
        return lag, "FRESH"
    if lag <= 7:
        return lag, "STALE_WARNING"
    return lag, "STALE_INVALID"


def calculate_metrics(records, index_code, index_name, as_of_date=None):
    as_of = as_of_date or dt.date.today()
    rows, warnings, extreme = _clean_records(records)
    count = len(rows)
    if count >= 500:
        ma_window, confidence = 500, "HIGH"
    elif count >= 250:
        ma_window, confidence = 250, "MEDIUM"
    elif count >= 120:
        ma_window, confidence = 120, "LOW"
    else:
        ma_window, confidence = None, "UNAVAILABLE"
    backfilled = bool(
        index_code == A500_CODE and rows and rows[0][0] < A500_LAUNCH_DATE
    )
    if backfilled:
        warnings.append("CONTAINS_BACKFILLED_HISTORY")
    result = {
        "indexCode": index_code, "indexName": index_name,
        "latestDate": rows[-1][0].isoformat() if rows else None,
        "latestClose": rows[-1][1] if rows else None,
        "sampleCount": count,
        "historyStartDate": rows[0][0].isoformat() if rows else None,
        "historyEndDate": rows[-1][0].isoformat() if rows else None,
        "movingAverageWindow": ma_window, "movingAverage": None,
        "movingAverageDistance": None, "oneYearHigh": None,
        "oneYearDrawdown": None, "volatilityWindow": 60,
        "annualizedVolatility": None, "dataFreshnessDays": None,
        "freshnessStatus": "UNAVAILABLE", "confidence": confidence,
        "isBackfilledHistory": backfilled, "warnings": warnings,
    }
    if not rows:
        result["warnings"].append("NO_VALID_PRICE_DATA")
        return result
    lag, freshness = _freshness(rows[-1][0], as_of)
    result.update({"dataFreshnessDays": lag, "freshnessStatus": freshness})
    if freshness == "STALE_INVALID":
        result["confidence"] = "UNAVAILABLE"
        result["warnings"].append("PRICE_DATA_STALE_INVALID")
    elif freshness == "STALE_WARNING":
        result["warnings"].append("PRICE_DATA_STALE_WARNING_NATURAL_DAY_APPROXIMATION")
    if extreme:
        result["confidence"] = "UNAVAILABLE"
    if ma_window:
        ma = sum(value for _, value in rows[-ma_window:]) / ma_window
        result["movingAverage"] = ma
        result["movingAverageDistance"] = rows[-1][1] / ma - 1
    if count >= 120:
        window = rows[-min(250, count):]
        high = max(value for _, value in window)
        result["oneYearHigh"] = high
        result["oneYearDrawdown"] = rows[-1][1] / high - 1
    if count >= 61:
        closes = [value for _, value in rows[-61:]]
        returns = [closes[i] / closes[i - 1] - 1 for i in range(1, 61)]
        mean = sum(returns) / len(returns)
        # Sample standard deviation (n-1), annualized with sqrt(252).
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        result["annualizedVolatility"] = math.sqrt(variance) * math.sqrt(252)
    return result


def _market_adjustment(metrics):
    if metrics.get("confidence") == "UNAVAILABLE":
        return 0.0, None
    ma_distance = metrics.get("movingAverageDistance")
    drawdown = metrics.get("oneYearDrawdown")
    if ma_distance is None or drawdown is None:
        return 0.0, None
    environment = (moving_average_score(ma_distance) + drawdown_score(drawdown)) / 2
    if environment <= 20:
        return -5.0, environment
    if environment <= 40:
        return -3.0, environment
    if environment <= 60:
        return 0.0, environment
    if environment <= 75:
        return 3.0, environment
    return 5.0, environment


def _level(score):
    if score <= 20:
        return "VERY_HOT"
    if score <= 40:
        return "HOT"
    if score <= 60:
        return "NEUTRAL"
    if score <= 75:
        return "COOL"
    if score <= 85:
        return "VERY_COOL"
    return "EXTREME_RISK"


def calculate_temperature(carrier, market):
    warnings = list(carrier.get("warnings", [])) + list(market.get("warnings", []))
    reasons = []
    if carrier.get("confidence") in ("UNAVAILABLE", "LOW"):
        reasons.append("A500价格样本置信度不足，温度仅展示，不调整资金释放。")
        return {
            "carrierIndex": carrier, "marketIndex": market,
            "opportunityScore": None, "volatilityPenalty": None,
            "marketAdjustment": 0.0, "finalScore": None,
            "level": "UNAVAILABLE", "releaseFactor": 1.0,
            "confidence": carrier.get("confidence", "UNAVAILABLE"),
            "reasons": reasons, "warnings": warnings,
            "formulaVersion": FORMULA_VERSION,
        }
    required = (
        carrier.get("movingAverageDistance"), carrier.get("oneYearDrawdown"),
        carrier.get("annualizedVolatility"),
    )
    if any(value is None for value in required):
        carrier = dict(carrier, confidence="UNAVAILABLE")
        return calculate_temperature(carrier, market)
    opportunity = (
        moving_average_score(carrier["movingAverageDistance"]) * 0.50
        + drawdown_score(carrier["oneYearDrawdown"]) * 0.50
    )
    penalty = volatility_penalty(carrier["annualizedVolatility"])
    adjustment, market_score = _market_adjustment(market)
    final = max(0.0, min(100.0, opportunity - penalty + adjustment))
    level = _level(final)
    release = {
        "VERY_HOT": 0.20, "HOT": 0.50, "NEUTRAL": 0.80,
        "COOL": 1.00, "VERY_COOL": 1.00, "EXTREME_RISK": 0.90,
    }[level]
    reasons.append("A500相对长期均线与近一年高点位置形成基础机会分。")
    reasons.append("60日实现波动率只作为风险惩罚，不作为机会加分。")
    if market_score is None:
        reasons.append("沪深300市场环境数据不可用，本次不做环境修正。")
    else:
        reasons.append("沪深300仅提供%+.0f分环境修正。" % adjustment)
    if level == "EXTREME_RISK":
        reasons.append("价格位置较低，但波动和不确定性可能较高，不代表已经见底。")
    return {
        "carrierIndex": carrier, "marketIndex": market,
        "opportunityScore": round(opportunity, 4),
        "volatilityPenalty": round(penalty, 4),
        "marketAdjustment": round(adjustment, 4),
        "finalScore": round(final, 4), "level": level,
        "releaseFactor": release, "confidence": carrier["confidence"],
        "reasons": reasons, "warnings": warnings,
        "formulaVersion": FORMULA_VERSION,
    }
