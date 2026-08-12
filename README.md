# homelab-ai-inference-lab
在RTX 3050Ti 4GB显存笔记本上，从零搭建AI推理可观测性系统：WSL2+Prometheus+Grafana+DCGM Exporter部署实录
# Homelab AI Inference Lab

> 在 RTX 3050Ti 4GB 显存笔记本上，从零搭建 AI 推理可观测性系统的完整实验记录。

## 项目背景

我是一名非科班出身的 00 后 FAE，因被 DeepSeek 的技术愿景打动，决定用实际项目来证明自己的动手能力。
这个仓库记录了我在**极限资源条件**（笔记本 4GB 显存）下，从零搭建 AI 推理可观测性系统的全过程——包括所有踩坑、排障和性能实验。


---

## 已完成工作

- [x] WSL2 GPU 直通配置
- [x] VMware 虚拟机部署监控节点
- [x] Prometheus + Grafana + DCGM Exporter 可观测性链路打通
- [x] Ollama 推理服务部署与梯度压测
- [x] vLLM 推理引擎安装与配置
- [ ] vLLM vs Ollama 对比压测实验（进行中）

---

## 技术栈

| 组件 | 用途 |
|------|------|
| VMware Workstation Pro | 监控节点虚拟化 |
| WSL2 Ubuntu 22.04 | 推理节点，GPU 直通 |
| Docker + Docker Compose | 容器化部署 |
| Prometheus | 指标采集与存储 |
| Grafana | 可视化看板 |
| DCGM Exporter | GPU 指标采集 |
| Ollama | 推理服务（已完成压测） |
| vLLM | 推理引擎（对比实验中） |

---

## 硬件环境

| 项目 | 配置 |
|------|------|
| 物理机 | 笔记本，i5-12700H，16GB DDR4 |
| 显卡 | RTX 3050Ti Laptop GPU，4GB 显存 |
| 监控节点 | VMware 虚拟机，Ubuntu 22.04，4GB 内存 |
| 推理节点 | WSL2 Ubuntu 22.04，直通 GPU |

> 4GB 显存是很多个人开发者的“穷鬼硬件”，本实验旨在探索极限资源下的 AI 推理可观测性方案。

---
