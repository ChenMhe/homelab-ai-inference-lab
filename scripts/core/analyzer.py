"""
=============================================================================
模块C：数据采集与分类层 (Analyzer)
=============================================================================
职责：
    1. 接收模块A产生的 final_report 原始数据。
    2. 对各个阶段的原始数据进行加工，计算补充指标。
    3. 组织成结构化的分析数据集，供模块D持久化。
=============================================================================
"""

from typing import Dict, Any, List, Optional
import statistics


def analyze(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    分析模块A的最终报告，生成结构化的分析数据。
    如果某个阶段未运行，对应字段将为空或None，不会报错。
    """
    result = {
        "status": report.get("status"),
        "memory_limit_chars": report.get("memory_limit_chars"),
        "concurrency_limit": report.get("concurrency_limit"),
        "rps_saturation": report.get("rps_at_saturation"),
    }

    # ---- 分析阶段二（并发测试） ----
    rps_history = report.get("rps_history", [])
    p99_history = report.get("p99_history", [])
    concurrency_data = []
    if rps_history and p99_history:
        base_p99 = p99_history[0][1] if p99_history else None
        prev_rps = None
        for (c, rps), (_, p99) in zip(rps_history, p99_history):
            growth_rate = None
            if prev_rps is not None and prev_rps > 0:
                growth_rate = (rps - prev_rps) / prev_rps
            p99_multiplier = None
            if base_p99 and base_p99 > 0:
                p99_multiplier = p99 / base_p99
            concurrency_data.append({
                "concurrency": c,
                "rps": rps,
                "p99_ms": p99,
                "rps_growth_rate": growth_rate,
                "p99_multiplier": p99_multiplier,
            })
            prev_rps = rps
    result["concurrency_analysis"] = concurrency_data

    # ---- 分析阶段三（正交扫描） ----
    orthogonal_raw = report.get("orthogonal_data", [])
    orthogonal_analysis = []
    for point in orthogonal_raw:
        analyzed_point = {
            "length": point.get("length"),
            "concurrency": point.get("concurrency"),
            "rps": point.get("rps"),
            "p99_ms": point.get("p99_ms"),
            "success_rate": point.get("success_rate"),
            "success_count": point.get("success_count"),
            "total_count": point.get("total_count"),
        }
        orthogonal_analysis.append(analyzed_point)
    result["orthogonal_analysis"] = orthogonal_analysis

    # ---- 分析阶段四（长稳测试） ----
    endurance = report.get("endurance")
    if endurance:
        endurance_analyzed = endurance.copy()
        if "is_pass" not in endurance_analyzed:
            overall_success_rate = endurance_analyzed.get("overall_success_rate", 0)
            p99_range = endurance_analyzed.get("p99_range_ms", 0)
            endurance_analyzed["is_pass"] = (overall_success_rate == 100.0) and (p99_range < 200.0)
        result["endurance_analysis"] = endurance_analyzed
    else:
        result["endurance_analysis"] = None

    # ---- 推荐配置 ----
    result["recommended_config"] = report.get("recommended_config")

    # ---- 生成摘要 ----
    summary = {}
    if result.get("memory_limit_chars"):
        summary["显存极限（字符数）"] = result["memory_limit_chars"]
    if result.get("concurrency_limit"):
        summary["并发极限"] = result["concurrency_limit"]
    if result.get("rps_saturation"):
        summary["饱和RPS"] = round(result["rps_saturation"], 3)
    if result.get("recommended_config"):
        rc = result["recommended_config"]
        summary["推荐配置"] = f"长度={rc.get('length')}, 并发={rc.get('concurrency')}"
    if result.get("endurance_analysis"):
        e = result["endurance_analysis"]
        summary["长稳通过"] = e.get("is_pass", False)
        summary["长稳总成功率"] = f"{e.get('overall_success_rate', 0):.2f}%"
        summary["P99抖动(极差)"] = f"{e.get('p99_range_ms', 0):.2f}ms"
    result["summary"] = summary

    return result