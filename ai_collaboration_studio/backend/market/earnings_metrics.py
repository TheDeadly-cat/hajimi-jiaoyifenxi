from __future__ import annotations

import copy
import hashlib
from typing import Any


METRICS_VERIFIED_AT = "2026-07-20"

_MU_DECK = "https://investors.micron.com/static-files/2354ecda-77a0-4ddd-8462-a631eb491356"
_SNDK_DECK = "https://investor.sandisk.com/static-files/8ea78860-f8e5-4f1c-ada3-c554437d6281"
_WDC_DECK = "https://investor.wdc.com/static-files/5b2d41c1-7d45-4575-b9ea-c51424dbffeb"
_STX_DECK = "https://s24.q4cdn.com/101481333/files/doc_financials/2026/q3/STX-FQ3-26-Supplemental.pdf"


CURATED_EARNINGS_METRICS: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("US.MU", "FY2026-Q3"): [
        {
            "metric_name": "DRAM bit shipments QoQ",
            "value_text": "up low-single digit percentage range",
            "direction": "up",
            "comparison_period": "QoQ",
            "fact_or_guidance": "historical_fact",
            "technology": "DRAM",
            "source_locator": "PDF page 22, Performance by technology",
            "source_url": _MU_DECK,
        },
        {
            "metric_name": "DRAM ASP QoQ",
            "value_text": "up low-60s percentage range",
            "direction": "up",
            "comparison_period": "QoQ",
            "fact_or_guidance": "historical_fact",
            "technology": "DRAM",
            "source_locator": "PDF page 22, Performance by technology",
            "source_url": _MU_DECK,
        },
        {
            "metric_name": "NAND bit shipments QoQ",
            "value_text": "up mid-single digit percentage range",
            "direction": "up",
            "comparison_period": "QoQ",
            "fact_or_guidance": "historical_fact",
            "technology": "NAND",
            "source_locator": "PDF page 22, Performance by technology",
            "source_url": _MU_DECK,
        },
        {
            "metric_name": "NAND ASP QoQ",
            "value_text": "up mid-80s percentage range",
            "direction": "up",
            "comparison_period": "QoQ",
            "fact_or_guidance": "historical_fact",
            "technology": "NAND",
            "source_locator": "PDF page 22, Performance by technology",
            "source_url": _MU_DECK,
        },
        {
            "metric_name": "2026 industry DRAM bit shipment growth outlook",
            "value_text": "low- to mid-20s percentage range",
            "direction": "up",
            "comparison_period": "CY2026 YoY",
            "fact_or_guidance": "company_guidance",
            "technology": "DRAM",
            "source_locator": "PDF page 16, Market outlook",
            "source_url": _MU_DECK,
        },
        {
            "metric_name": "2026 industry NAND bit shipment growth outlook",
            "value_text": "approximately 20%",
            "numeric_value": 20.0,
            "unit": "percent",
            "direction": "up",
            "comparison_period": "CY2026 YoY",
            "fact_or_guidance": "company_guidance",
            "technology": "NAND",
            "source_locator": "PDF page 16, Market outlook",
            "source_url": _MU_DECK,
        },
    ],
    ("US.SNDK", "FY2026-Q3"): [
        {
            "metric_name": "Datacenter revenue",
            "value_text": "$1,467 million; up 233% QoQ",
            "numeric_value": 1467.0,
            "unit": "USD million",
            "direction": "up",
            "comparison_period": "QoQ",
            "fact_or_guidance": "historical_fact",
            "technology": "NAND",
            "source_locator": "PDF page 6, Revenue Trends by End Market",
            "source_url": _SNDK_DECK,
        },
        {
            "metric_name": "Edge revenue",
            "value_text": "$3,663 million; up 118% QoQ",
            "numeric_value": 3663.0,
            "unit": "USD million",
            "direction": "up",
            "comparison_period": "QoQ",
            "fact_or_guidance": "historical_fact",
            "technology": "NAND",
            "source_locator": "PDF page 6, Revenue Trends by End Market",
            "source_url": _SNDK_DECK,
        },
        {
            "metric_name": "Consumer revenue",
            "value_text": "$820 million; down 10% QoQ",
            "numeric_value": 820.0,
            "unit": "USD million",
            "direction": "down",
            "comparison_period": "QoQ",
            "fact_or_guidance": "historical_fact",
            "technology": "NAND",
            "source_locator": "PDF page 6, Revenue Trends by End Market",
            "source_url": _SNDK_DECK,
        },
        {
            "metric_name": "Q4 FY2026 revenue guidance",
            "value_text": "$7,750 million to $8,250 million",
            "unit": "USD million",
            "direction": "range",
            "comparison_period": "FY2026-Q4",
            "fact_or_guidance": "company_guidance",
            "technology": "NAND",
            "source_locator": "PDF page 9, Fiscal Fourth Quarter Guidance",
            "source_url": _SNDK_DECK,
        },
    ],
    ("US.WDC", "FY2026-Q3"): [
        {
            "metric_name": "Total HDD exabytes shipped",
            "value_text": "222 EB",
            "numeric_value": 222.0,
            "unit": "EB",
            "direction": "up",
            "comparison_period": "FY2026-Q3",
            "fact_or_guidance": "historical_fact",
            "technology": "HDD",
            "source_locator": "PDF page 5, Business Metrics - Exabytes",
            "source_url": _WDC_DECK,
        },
        {
            "metric_name": "Nearline HDD exabytes shipped",
            "value_text": "199 EB",
            "numeric_value": 199.0,
            "unit": "EB",
            "direction": "up",
            "comparison_period": "FY2026-Q3",
            "fact_or_guidance": "historical_fact",
            "technology": "HDD",
            "source_locator": "PDF page 5, Business Metrics - Exabytes",
            "source_url": _WDC_DECK,
        },
        {
            "metric_name": "Latest-generation ePMR units shipped",
            "value_text": "4.1 million units",
            "numeric_value": 4.1,
            "unit": "million units",
            "direction": "not_stated",
            "comparison_period": "FY2026-Q3",
            "fact_or_guidance": "historical_fact",
            "technology": "HDD",
            "source_locator": "PDF page 3, Q3FY26 Highlights",
            "source_url": _WDC_DECK,
        },
        {
            "metric_name": "Q4 FY2026 revenue guidance",
            "value_text": "$3.65 billion plus or minus $100 million",
            "numeric_value": 3.65,
            "unit": "USD billion midpoint",
            "direction": "range",
            "comparison_period": "FY2026-Q4",
            "fact_or_guidance": "company_guidance",
            "technology": "HDD",
            "source_locator": "PDF page 7, Q4FY26 Guidance",
            "source_url": _WDC_DECK,
        },
    ],
    ("US.STX", "FY2026-Q3"): [
        {
            "metric_name": "Total HDD exabytes shipped",
            "value_text": "199 EB; up 39% YoY",
            "numeric_value": 199.0,
            "unit": "EB",
            "direction": "up",
            "comparison_period": "YoY",
            "fact_or_guidance": "historical_fact",
            "technology": "HDD",
            "source_locator": "PDF page 4, Markets and Technology Highlights",
            "source_url": _STX_DECK,
        },
        {
            "metric_name": "Nearline HDD exabytes shipped",
            "value_text": "175 EB",
            "numeric_value": 175.0,
            "unit": "EB",
            "direction": "up",
            "comparison_period": "FY2026-Q3",
            "fact_or_guidance": "historical_fact",
            "technology": "HDD",
            "source_locator": "PDF page 6, Quarterly Financial Trends Continued",
            "source_url": _STX_DECK,
        },
        {
            "metric_name": "Q4 FY2026 revenue guidance",
            "value_text": "$3.45 billion plus or minus $100 million",
            "numeric_value": 3.45,
            "unit": "USD billion midpoint",
            "direction": "range",
            "comparison_period": "FY2026-Q4",
            "fact_or_guidance": "company_guidance",
            "technology": "HDD",
            "source_locator": "PDF page 7, Guidance Q4FY26",
            "source_url": _STX_DECK,
        },
    ],
}


def official_earnings_metrics(symbol: str, fiscal_period: str) -> list[dict[str, Any]]:
    metrics = copy.deepcopy(CURATED_EARNINGS_METRICS.get((symbol, fiscal_period), []))
    for metric in metrics:
        stable_key = f"{symbol}|{fiscal_period}|{metric.get('metric_name')}|{metric.get('source_locator')}"
        metric["metric_id"] = "metric_" + hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:20]
        metric["symbol"] = symbol
        metric["fiscal_period"] = fiscal_period
        metric["source_type"] = "company_ir"
        metric["source_tier"] = "primary"
        metric["claim_status"] = "company_statement"
        metric["verification_method"] = "manual_source_locator_review"
        metric["verified_at"] = METRICS_VERIFIED_AT
        metric["execution_capability"] = "none"
        metric["live_trading_allowed"] = False
    return metrics
