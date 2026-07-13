import datetime as dt
import json
from pathlib import Path

import akshare as ak
from utils import output_paths


BASE_DIR = Path(__file__).resolve().parent


def percentile(series):
    current = float(series.iloc[-1])
    valid = series.dropna().astype(float)
    return round(float((valid <= current).sum() / len(valid) * 100), 4)


def main():
    run_dir = output_paths.create_run_dir("phase-p1-valuation")
    sample_path = output_paths.get_csv_path("a-share-valuation-sample.csv", run_dir)
    result_path = output_paths.get_json_path("a-share-valuation-result.json", run_dir)
    report_path = output_paths.get_report_path("a-share-valuation-source-feasibility.md", run_dir)
    pe = ak.stock_index_pe_lg(symbol="沪深300")
    pb = ak.stock_index_pb_lg(symbol="沪深300")
    sample = pe[["日期", "滚动市盈率"]].merge(
        pb[["日期", "市净率"]], on="日期", how="inner"
    )
    sample = sample.rename(columns={
        "日期": "date", "滚动市盈率": "pe_ttm", "市净率": "pb"
    }).sort_values("date")
    sample.to_csv(sample_path, index=False, encoding="utf-8")

    a500_errors = []
    for function_name, function in (
        ("stock_index_pe_lg", ak.stock_index_pe_lg),
        ("stock_index_pb_lg", ak.stock_index_pb_lg),
    ):
        try:
            function(symbol="中证A500")
        except Exception as exc:
            a500_errors.append(
                f"{function_name}: {type(exc).__name__}: {exc}"
            )

    hs300 = {
        "index_name": "沪深300",
        "index_code": "000300",
        "source": "AKShare stock_index_pe_lg / stock_index_pb_lg (upstream: Legulegu)",
        "source_url_or_api": [
            "https://akshare.akfamily.xyz/data/stock/stock.html",
            "https://legulegu.com/stockdata/hs300-ttm-lyr",
            "https://legulegu.com/stockdata/hs300-pb",
        ],
        "official_source": False,
        "can_fetch_current_value": True,
        "can_fetch_history": True,
        "earliest_date": str(sample.iloc[0]["date"]),
        "latest_date": str(sample.iloc[-1]["date"]),
        "sample_size": int(len(sample)),
        "fields_available": ["PE_TTM", "PB"],
        "update_frequency": "Daily trading-day observations",
        "reproducible": True,
        "confidence": "Medium",
        "limitations": [
            "AKShare wrapper is reproducible but the upstream is a third party, not CSI official data.",
            "Field definitions must be pinned to weighted rolling PE and weighted PB before model use.",
            "Current project runtime is Python 3.7; current AKShare requires Python 3.9+.",
        ],
        "local_pe_percentile": percentile(sample["pe_ttm"]),
        "local_pb_percentile": percentile(sample["pb"]),
        "sample_window": f"{sample.iloc[0]['date']} to {sample.iloc[-1]['date']}",
        "calculation_method": "rank = count(value <= current) / valid sample count",
        "execute_eligibility": "CANDIDATE_ONLY_SOURCE_REVIEW_REQUIRED",
    }
    a500 = {
        "index_name": "中证A500",
        "index_code": "000510",
        "source": "CSI official public endpoints + AKShare feasibility test",
        "source_url_or_api": [
            "https://www.csindex.com.cn/csindex-home/perf/indexCsiDsPe",
            "AKShare stock_index_pe_lg / stock_index_pb_lg",
        ],
        "official_source": "Partial endpoint only",
        "can_fetch_current_value": True,
        "can_fetch_history": False,
        "earliest_date": None,
        "latest_date": None,
        "sample_size": 0,
        "fields_available": ["official endpoint: peg only; PE_TTM meaning not documented in response", "PB unavailable"],
        "update_frequency": "Unknown for a complete PE_TTM/PB pair",
        "reproducible": False,
        "confidence": "Low",
        "limitations": [
            "AKShare Legulegu symbol list does not include 中证A500.",
            "CSI official public history endpoint returns a single peg field and no PB history.",
            "No complete, field-defined PE_TTM/PB history was validated.",
        ] + a500_errors,
        "local_pe_percentile": None,
        "local_pb_percentile": None,
        "sample_window": None,
        "calculation_method": None,
        "execute_eligibility": "NOT_ELIGIBLE",
    }
    result = {
        "phase": 2,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "WARNING",
        "warning_code": "A500_VALUATION_NOT_AVAILABLE",
        "requires_user_confirmation": True,
        "next_phase_allowed": False,
        "sample_thresholds": {
            "below_250": "NOT_ELIGIBLE_FOR_EXECUTE",
            "250_to_749": "REFERENCE_ONLY",
            "750_or_more": "CANDIDATE_USED_IN_SCORE_AFTER_SOURCE_REVIEW",
        },
        "indices": {"hs300": hs300, "a500": a500},
    }
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# A-Share Valuation Source Feasibility", "",
        "## Phase Result", "",
        "- Status: **WARNING**",
        "- Warning: `A500_VALUATION_NOT_AVAILABLE`",
        "- Next Phase Allowed: **No — user confirmation required**",
        "- Existing third-party percentile values remain prohibited as model inputs.",
        "",
        "## Feasibility Matrix", "",
        "| Index | Source | Official | Current | History | Earliest | Latest | Samples | Fields | Reproducible | Confidence |",
        "|---|---|---|---:|---:|---|---|---:|---|---:|---|",
        f"| 沪深300 | AKShare / Legulegu | No | Yes | Yes | {hs300['earliest_date']} | {hs300['latest_date']} | {hs300['sample_size']} | PE_TTM, PB | Yes | Medium |",
        "| 中证A500 | CSI partial endpoint + AKShare test | Partial | Yes | No | - | - | 0 | `peg` only; PB missing | No | Low |",
        "",
        "## Local Percentile Recalculation", "",
        f"- HS300 PE_TTM percentile: **{hs300['local_pe_percentile']:.4f}%**",
        f"- HS300 PB percentile: **{hs300['local_pb_percentile']:.4f}%**",
        f"- Sample window: {hs300['sample_window']}",
        f"- Sample size: {hs300['sample_size']}",
        f"- Method: `{hs300['calculation_method']}`",
        "- Eligibility: sample threshold is met, but source is third-party and therefore remains candidate-only pending source approval.",
        "",
        "## Limitations and Stop Decision", "",
        "沪深300满足历史序列和样本量门槛，但 AKShare 明确显示上游为乐咕乐股，不是中证官方数据。中证A500未验证到同时包含 PE_TTM 与 PB 的历史序列。根据阶段规则，Phase 2 在此暂停，不进入宏观数据审计，也不重构决策门。",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output_paths.write_run_manifest({
        "phase": "phase-p1-valuation", "task_name": "A-share valuation source feasibility",
        "decision_status": "NOT_RUN", "data_status": result.get("status", "WARNING"),
        "model_status": "NOT_RUN",
        "output_files": ["reports/a-share-valuation-source-feasibility.md", "json/a-share-valuation-result.json", "csv/a-share-valuation-sample.csv"],
        "blocked_reason": result.get("warning_code") or "",
        "next_action": "Review source feasibility",
        "source_data_used": ["AKShare", "Legulegu"],
        "whether_root_directory_was_modified": "No",
    }, run_dir)
    print("Phase 2: WARNING")
    print("WARNING: A500_VALUATION_NOT_AVAILABLE")
    print("STOP: user confirmation required before Phase 3")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        output_paths.write_blocked_outputs(exc, {"phase": "phase-p1-valuation", "task_name": "A-share valuation source feasibility"})
        raise
