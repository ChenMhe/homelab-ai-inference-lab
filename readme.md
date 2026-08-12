# Homelab AI Inference Lab

> 在 RTX 3050Ti 4GB 显存笔记本上，从零搭建 AI 推理可观测性系统的完整实验记录。

---

## 硬件环境

| 项目 | 配置 |
|------|------|
| 物理机 | 笔记本，i5-12700H，16GB DDR4，RTX 3050Ti 4GB，Windows 11 |
| 监控节点 | VMware 虚拟机，Ubuntu 22.04，4GB 内存，2 核 CPU |
| 推理节点 | WSL2 Ubuntu 22.04，直通物理机 GPU（4GB 显存） |

---

## 已完成工作

- [x] WSL2 GPU 直通配置
- [x] VMware 虚拟机部署监控节点
- [x] Prometheus + Grafana + DCGM Exporter 可观测性链路打通
- [x] Ollama 推理服务部署与梯度压测
- [x] vLLM 推理引擎安装与配置（对比实验进行中）

---

## 项目结构
homelab-ai-inference-lab/
├── README.md
├── LICENSE
├── configs/ # 部署配置文件
│ ├── docker-compose.yml
│ └── prometheus.yml
├── docs/ # 实验文档
│ ├── 01-deploy-monitoring.md # 部署与监控接入记录
│ ├── 02-deploy-LLM.md # Ollama 压测实验记录
│ ├── deepseek-r1-GPU硬件指标.xlsx # GPU 指标原始数据
│ ├── deepseek-r1的输入可调参数.xlsx # 输入参数原始数据
│ └── deepseek-r1软件层输出结果指标.xlsx # 软件层指标原始数据
├── images/ # 文档配图（所有截图）
│ └── ...
└── scripts/ # 压测脚本
├── config.yaml # 压测配置文件
├── core/ # 核心逻辑
│ ├── init.py
│ ├── analyzer.py
│ ├── controller.py
│ ├── exporter.py
│ └── requester.py
├── utils/ # 工具函数
│ ├── init.py
│ ├── config_loader.py
│ └── prompt_generator.py
└── prompts/ # 提示词模板

> `.vscode/`、`doc原文记录版/`、`config - 副本.yaml` 为本地临时文件，不上传仓库。  
> `scripts/core/output/` 和 `concurrency_results.csv` 为运行时生成的实验数据，不纳入版本管理。

---

## 文档索引

| 文档 | 说明 | 状态 |
|------|------|------|
| [部署与监控接入记录](./docs/01-deploy-monitoring.md) | 完整部署过程 + 排障实录 | ✅ 已完成 |
| [Ollama 压测实验记录](./docs/02-deploy-LLM.md) | 梯度压测与性能拐点分析 | ✅ 已完成 |
| vLLM vs Ollama 对比实验 | 对比分析报告 | 🔄 进行中 |

---

## 核心结论

### Ollama 压测结果

| 测试项 | 结果 |
|--------|------|
| 显存上下文极限 | `num_ctx: 98000`（约 3.8GB） |
| 有效并发上限 | **1**（超过后吞吐不增，P99 延迟线性倍增） |
| 单请求 RPS | ~0.43 req/s（256 Token 输出） |
| 显存管理机制 | 预分配 KV Cache + 自动卸载层到内存 |
| 稳定性 | 短文本（2000 字符）可通过 10 分钟长稳测试 |

**关键发现**：Ollama 在 4GB 显存下会主动将部分模型层卸载到系统内存（CPU），牺牲性能换取不崩溃。这使其几乎不会触发显存 OOM，符合“安全部署”的设计定位。

---

## 关于作者

欢迎通过 GitHub 私信交流。