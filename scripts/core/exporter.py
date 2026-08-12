"""
=============================================================================
模块D：持久化层 (Exporter)
=============================================================================
职责：
    1. 接收模块C生成的分析数据。
    2. 将分析数据写入CSV和JSON文件。
    3. 自动创建输出目录，文件按阶段分类命名。
=============================================================================
"""

import os
import json
import csv
from datetime import datetime
from typing import Dict, Any, List, Optional


def export(analyzed_data: Dict[str, Any], output_dir: str = "./output") -> Dict[str, str]:
    """
    导出分析数据到CSV和JSON文件。

    参数：
        analyzed_data : 模块C返回的分析数据字典
        output_dir    : 输出目录，默认为 ./output

    返回：
        文件路径字典，包含每个导出文件的路径
    """
    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)

    # 生成时间戳
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"test_{timestamp}"

    file_paths = {}

    # ---- 导出摘要（JSON） ----
    summary_path = os.path.join(output_dir, f"{prefix}_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(analyzed_data.get("summary", {}), f, ensure_ascii=False, indent=2)
    file_paths["summary_json"] = summary_path

    # ---- 导出完整报告（JSON） ----
    full_path = os.path.join(output_dir, f"{prefix}_full_report.json")
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(analyzed_data, f, ensure_ascii=False, indent=2, default=str)
    file_paths["full_json"] = full_path

    # ---- 导出并发分析（CSV） ----
    concurrency_data = analyzed_data.get("concurrency_analysis", [])
    if concurrency_data:
        csv_path = os.path.join(output_dir, f"{prefix}_concurrency.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            if concurrency_data:
                writer = csv.DictWriter(f, fieldnames=concurrency_data[0].keys())
                writer.writeheader()
                writer.writerows(concurrency_data)
        file_paths["concurrency_csv"] = csv_path

    # ---- 导出正交分析（CSV） ----
    orthogonal_data = analyzed_data.get("orthogonal_analysis", [])
    if orthogonal_data:
        csv_path = os.path.join(output_dir, f"{prefix}_orthogonal.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            if orthogonal_data:
                writer = csv.DictWriter(f, fieldnames=orthogonal_data[0].keys())
                writer.writeheader()
                writer.writerows(orthogonal_data)
        file_paths["orthogonal_csv"] = csv_path

    # ---- 导出长稳分析（CSV） ----
    endurance = analyzed_data.get("endurance_analysis")
    if endurance:
        # 长稳测试的数据是单行汇总，这里导出为单行CSV便于阅读
        csv_path = os.path.join(output_dir, f"{prefix}_endurance.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            # 将字典展开为单行
            flat = _flatten_dict(endurance)
            writer = csv.DictWriter(f, fieldnames=flat.keys())
            writer.writeheader()
            writer.writerow(flat)
        file_paths["endurance_csv"] = csv_path

    # ---- 导出推荐配置（单独JSON，便于解析） ----
    recommended = analyzed_data.get("recommended_config")
    if recommended:
        rec_path = os.path.join(output_dir, f"{prefix}_recommended_config.json")
        with open(rec_path, "w", encoding="utf-8") as f:
            json.dump(recommended, f, ensure_ascii=False, indent=2)
        file_paths["recommended_json"] = rec_path

    # 打印导出位置
    print("\n[模块D] 文件已导出到:")
    for key, path in file_paths.items():
        print(f"  - {key}: {path}")

    return file_paths


def _flatten_dict(d: Dict[str, Any], parent_key: str = "", sep: str = "_") -> Dict[str, Any]:
    """
    递归展开嵌套字典，用于CSV单行导出。
    """
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(_flatten_dict(v, new_key, sep=sep).items())
        elif isinstance(v, list):
            # 列表转为字符串
            items.append((new_key, json.dumps(v, ensure_ascii=False)))
        else:
            items.append((new_key, v))
    return dict(items)