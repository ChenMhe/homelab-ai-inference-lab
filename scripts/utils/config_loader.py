"""
配置加载器
用法：config = load_config("config.yaml")
"""

import yaml
from pathlib import Path

def load_config(config_path="config.yaml"):
    # 获取当前脚本所在目录的上一级（即 scripts 目录）
    base_dir = Path(__file__).parent.parent
    config_file = base_dir / config_path

    if not config_file.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_file}")

    with open(config_file, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)