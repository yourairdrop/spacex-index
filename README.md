# SpaceX 指标面板 · SPCX Index Dashboard

围绕 SpaceX IPO（纳斯达克代码 **SPCX**）的实时监控面板：聚合与 SpaceX 关联的股票、用代理标的反推市场情绪、估算模型合理价，并量化其 AI 算力叙事。

> ⚠️ 仅供研究，不构成投资建议。行情有约 1–4 秒延迟，部分基本面为手动维护的静态数字。

## 功能

- **实时行情** — 通过 Yahoo v8 端点（约 1–4 秒延迟）采集 20+ 只关联标的，仅记录交易时段
- **关联标的分组**
  - *股权关联* — 直接/间接持有 SpaceX 股权（GOOGL、DXYZ、NASA、XOVR、RONB、VCX）
  - *算力供应链* — Colossus 超算供应商（NVDA、DELL、SMCI、TSLA、VRT、AVGO）
  - *算力竞品* — SpaceX 出租算力的对手（CRWV、NBIS）
  - *情绪篮子* — 太空板块光环交易（ASTS、RKLB、RDW、SATS、SPCE、LUNR、PL）
- **综合情绪指标 (0–100)** — DXYZ 对 NAV 溢价 z 分 + 太空篮子 β 剥离残差 z 分 + 代理基金成交量 z 分，经正态 CDF 合成
- **模型合理价** — 以 IPO 询价为锚，叠加大盘 β 与板块超额，独立于实际成交价判断高估/低估
- **算力叙事量化** — 已签算力合同年化 vs 分部收入，实时 P/S
- **交互图表** — 滚轮缩放 / 拖拽平移，intraday 历史回算 + 实时采样，仅显示交易时段

## 技术栈

Python + Flask（后端采集 / API）· yfinance（日线历史）· Chart.js（前端图表）· SQLite（时序快照）

## 运行

```bash
python3 -m venv .venv
.venv/bin/pip install yfinance flask
.venv/bin/python app.py
# 打开 http://localhost:8500
```

## 结构

| 文件 | 作用 |
|------|------|
| `engine.py` | 指标计算引擎：行情采集、情绪指标、合理价模型、intraday 回算、基本面配置（顶部常量） |
| `app.py` | Flask 服务 + 后台采集线程 + SQLite 快照 + 盘后过滤 |
| `static/index.html` | 前端（卡片、图表、表格、算力叙事模块） |

## 数据源与维护

行情来自 Yahoo Finance 公开端点，自动更新。部分基本面数字（基金 NAV、各基金 SpaceX 敞口、SpaceX 分部收入、算力合同）为手动维护的静态配置，集中在 `engine.py` 顶部常量，需在新财报/新合同后更新。
