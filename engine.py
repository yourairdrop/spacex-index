"""SpaceX 指标引擎：行情采集 + 指标计算。

数据源 yfinance（约 15 分钟延迟）。两层指标：
  1. 敞口/溢价监控 —— DXYZ 对 NAV 溢价、GOOGL 持股价值、各代理基金敞口
  2. 情绪综合指标 —— DXYZ 溢价 z 分 + 情绪篮子β剥离残差 z 分 + 关注度 z 分 → 0-100
"""
import math
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf

_YHEADERS = {"User-Agent": "Mozilla/5.0"}
_session = requests.Session()

# ---- 静态配置（来源见注释，数据有"截至日期"，需人工跟进更新） ----
SPCX_IPO_PRICE = 135.0
GOOGL_SPACEX_STAKE = 0.05      # xAI 合并稀释后约 5%（2026-02，Bloomberg/Alaska filing 6.11% -> ~5%）
DXYZ_NAV = 24.56               # 2026-03-31 官方披露 NAV/股
DXYZ_NAV_ASOF = "2026-03-31"

EXPOSURES = {  # SpaceX 占基金组合比例（披露值）
    "NASA": {"pct": 0.1765, "asof": "2026-07-09", "note": "Tema Space Innovators，公开ETF中最高",
             "note_en": "Tema Space Innovators, highest among public ETFs"},
    "XOVR": {"pct": None, "asof": "2026-05-21", "note": "经 SPV 持有约 $2.92 亿 SpaceX",
             "note_en": "~$292M SpaceX via SPV"},
    "RONB": {"pct": 0.02, "asof": "2026-05", "note": "Baron First Principles",
             "note_en": "Baron First Principles"},
    "DXYZ": {"pct": 0.162, "asof": "2026-03-31", "note": "封闭式基金，第一大持仓",
             "note_en": "Closed-end fund, top holding"},
    "VCX":  {"pct": None, "asof": None, "note": "SpaceX/Anthropic 未上市股权代理",
             "note_en": "Pre-IPO SpaceX/Anthropic proxy"},
}

GROUPS = {
    "core": ["SPCX"],
    "holders": ["GOOGL", "NASA", "XOVR", "RONB", "DXYZ", "VCX"],
    "compute": ["NVDA", "DELL", "SMCI", "TSLA", "VRT", "AVGO"],   # 算力供应链（Colossus 超算 + 太空数据中心）
    "compute_rival": ["CRWV", "NBIS"],   # 算力竞品（SpaceX 出租算力的对手）
    "basket": ["ASTS", "RKLB", "RDW", "SATS", "SPCE", "LUNR", "PL"],   # 情绪篮子
    "ecosystem": ["HOOD"],
    "benchmark": ["SPY"],
}
ALL_TICKERS = [t for g in GROUPS.values() for t in g]

NAMES = {
    "SPCX": "SpaceX", "GOOGL": "Alphabet", "NASA": "Tema Space ETF",
    "XOVR": "ERShares Crossover", "RONB": "Baron First Principles",
    "DXYZ": "Destiny Tech100", "VCX": "VCX 未上市代理",
    "ASTS": "AST SpaceMobile", "RKLB": "Rocket Lab", "RDW": "Redwire",
    "SATS": "EchoStar", "SPCE": "Virgin Galactic", "LUNR": "Intuitive Machines",
    "PL": "Planet Labs", "HOOD": "Robinhood", "SPY": "S&P 500 ETF",
    "NVDA": "英伟达", "CRWV": "CoreWeave", "DELL": "戴尔", "SMCI": "超微",
    "TSLA": "特斯拉", "VRT": "Vertiv", "AVGO": "博通", "NBIS": "Nebius",
}

NAMES_EN = {
    "SPCX": "SpaceX", "GOOGL": "Alphabet", "NASA": "Tema Space ETF",
    "XOVR": "ERShares Crossover", "RONB": "Baron First Principles",
    "DXYZ": "Destiny Tech100", "VCX": "VCX pre-IPO proxy",
    "ASTS": "AST SpaceMobile", "RKLB": "Rocket Lab", "RDW": "Redwire",
    "SATS": "EchoStar", "SPCE": "Virgin Galactic", "LUNR": "Intuitive Machines",
    "PL": "Planet Labs", "HOOD": "Robinhood", "SPY": "S&P 500 ETF",
    "NVDA": "NVIDIA", "CRWV": "CoreWeave", "DELL": "Dell", "SMCI": "Supermicro",
    "TSLA": "Tesla", "VRT": "Vertiv", "AVGO": "Broadcom", "NBIS": "Nebius",
}

# 各标的与 SpaceX 的绑定原因（持股比例已核实，篮子归类源自媒体 sympathy-plays 报道）
BIND_REASON = {
    "SPCX": "SpaceX 本尊，2026-06-12 纳斯达克上市（与 xAI 合并后兼具 AI 属性）；7/7 已纳入纳斯达克100",
    "GOOGL": "双重绑定：① 2015 年起持股约 5%（IPO 后值 $1000 亿+）；② 每月 $9.2 亿 ×32 月租用 SpaceX/xAI 算力（既是股东又是大客户）",
    "NASA": "Tema 太空 ETF，约 13.3% 仓位为 SpaceX，公开 ETF 中持仓最高",
    "XOVR": "ERShares，经 SPV 持有约 $2.9 亿 SpaceX 未上市股权",
    "RONB": "Baron 基金，约 2% 仓位为 SpaceX（第一大持仓是特斯拉）",
    "DXYZ": "封闭式基金，约 16.2% 仓位为 SpaceX，第一大持仓（故市价对 NAV 长期溢价）",
    "VCX": "持有 SpaceX / Anthropic 等未上市股权的代理基金",
    "ASTS": "卫星直连手机，且用 SpaceX 猎鹰 9 号发射；被当作 SpaceX 情绪出口",
    "RKLB": "火箭发射公司，市场视为「小号 SpaceX」，板块龙头",
    "RDW": "Redwire 太空基础设施供应商，太空板块 sympathy 票",
    "SATS": "EchoStar 卫星通信，与 Starlink 同赛道竞合，sympathy 联动",
    "SPCE": "维珍银河太空旅游，纯情绪光环票（基本面很弱）",
    "LUNR": "Intuitive Machines 月球着陆器，NASA 合作，太空板块联动",
    "PL": "Planet Labs 卫星对地成像，太空板块联动",
    "HOOD": "Robinhood 拿到 IPO 承销资格，散户打新主通道，流量受益",
    "NVDA": "Colossus 用约 23 万英伟达 GPU（150K H100+50K H200+30K GB200），扩张目标百万级——最核心 GPU 供应商",
    "DELL": "为 Colossus 供应约一半服务器机架（PowerEdge XE9680，专为 NVIDIA HGX 优化）",
    "SMCI": "为 Colossus 供应另一半液冷机架（超微的液冷与机架集成专长）",
    "TSLA": "xAI 用 Tesla Megapack 为 Colossus 储能供电（近期再购 $2.69 亿，见 S1）；马斯克系协同",
    "VRT": "AI 数据中心液冷/供配电基础设施龙头，高密度 GPU 集群散热刚需（行业关联）",
    "AVGO": "xAI 在博通定制 AI 芯片(XPU) 客户管线中；并供 AI 集群网络芯片",
    "CRWV": "GPU 云租赁龙头；SpaceX 对外出租 Colossus 算力（Google/Anthropic 为客户），正面抢其生意（竞品）",
    "NBIS": "GPU 云同业（neocloud）；SpaceX 入局算力出租对其形成竞争（竞品）",
    "SPY": "标普 500 ETF，仅作大盘基准（用于剥离 β），本身不绑定 SpaceX",
}

BIND_REASON_EN = {
    "SPCX": "SpaceX itself — Nasdaq debut 2026-06-12 (also an AI company post-xAI merger); joined the Nasdaq-100 on Jul 7",
    "GOOGL": "Dual link: ① ~5% equity stake since 2015 (worth $100B+ post-IPO); ② $920M/mo × 32-month compute rental — both shareholder and major customer",
    "NASA": "Tema Space Innovators ETF, ~17.7% in SpaceX — largest allocation among public ETFs",
    "XOVR": "ERShares ETF, holds ~$292M of SpaceX via an SPV",
    "RONB": "Baron fund, ~2% in SpaceX (top holding is Tesla)",
    "DXYZ": "Closed-end fund, ~16.2% in SpaceX as top holding — hence its persistent premium to NAV",
    "VCX": "Proxy fund holding pre-IPO stakes in SpaceX / Anthropic",
    "ASTS": "Direct-to-cell satellites launched on Falcon 9; traded as a SpaceX sentiment outlet",
    "RKLB": "Rocket launch company, viewed as a 'mini SpaceX' — sector leader",
    "RDW": "Redwire, space-infrastructure supplier; sector sympathy play",
    "SATS": "EchoStar satellite comms, co-opetition with Starlink; sympathy-linked",
    "SPCE": "Virgin Galactic space tourism — pure halo trade (weak fundamentals)",
    "LUNR": "Intuitive Machines lunar landers, NASA contractor; sector-linked",
    "PL": "Planet Labs Earth-imaging satellites; sector-linked",
    "HOOD": "Robinhood won IPO underwriting — the main retail on-ramp for SPCX flow",
    "NVDA": "Colossus runs ~230K NVIDIA GPUs (150K H100 + 50K H200 + 30K GB200), scaling toward 1M+ — the core GPU supplier",
    "DELL": "Supplies about half of Colossus server racks (PowerEdge XE9680, optimized for NVIDIA HGX)",
    "SMCI": "Supplies the other half — liquid-cooled racks (Supermicro's specialty)",
    "TSLA": "xAI powers Colossus with Tesla Megapacks (fresh $269M purchase per the S-1); Musk-ecosystem synergy",
    "VRT": "Liquid-cooling / power-distribution leader for AI data centers — industry linkage",
    "AVGO": "xAI sits in Broadcom's custom AI-chip (XPU) pipeline; also supplies cluster networking silicon",
    "CRWV": "GPU-cloud leader; SpaceX renting out Colossus (Google/Anthropic as clients) competes head-on (rival)",
    "NBIS": "GPU-cloud peer (neocloud); pressured by SpaceX entering compute leasing (rival)",
    "SPY": "S&P 500 ETF — market benchmark for beta-stripping only; no SpaceX link",
}

# ---- SpaceX 基本面（S1 招股书 2026-05-20 披露 FY2025；算力合同来自公开新闻）----
# 静态数据，新财报 / 新合同后需人工更新
FUNDAMENTALS = {
    "asof": "FY2025 · S1(2026-05-20)",
    "revenue_total": 18.7,    # $B，2025 全公司收入
    "seg_starlink": 11.4,     # Starlink 连接
    "seg_launch": 4.0,        # 发射 + NASA 载人
    "seg_ai": 3.2,            # AI / xAI 分部
    "ai_op_loss": -6.35,      # AI 分部 2025 经营亏损 $B
    "compute_contracts": [    # 已签算力合同（前瞻年化）
        {"name": "Google", "monthly_b": 0.92, "until": "2029-06"},
        {"name": "Anthropic", "monthly_b": 1.25, "until": "2029-05"},
        {"name": "Reflection AI", "monthly_b": 0.15, "until": "2029"},  # 2026-06-22 签约，7/1 起 Colossus 2/GB300
    ],
}

SPCX_BETA_SHRINK = 0.4   # SPCX β 向 1 收缩系数（巨头波动更接近大盘）
SECTOR_LAMBDA = 0.3      # 板块情绪向 SPCX 合理价的传导系数
SPCX_BETA_CAP = 1.5     # SPCX β 上限（巨头不应有小盘太空股的极端β）
BETA_WINDOW = 60   # β回归窗口（交易日）
Z_WINDOW = 60      # z 分参考窗口
VOL_WINDOW = 20    # 成交量热度窗口


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _narrative(spcx_mcap):
    """算力叙事基本面快照：已签算力合同年化 vs 当前分部收入。"""
    F = FUNDAMENTALS
    comp_ann = sum(c["monthly_b"] * 12 for c in F["compute_contracts"])
    return {
        "asof": F["asof"],
        "revenue_total": F["revenue_total"],
        "segments": [
            {"name": "Starlink", "name_en": "Starlink", "rev": F["seg_starlink"]},
            {"name": "发射", "name_en": "Launch", "rev": F["seg_launch"]},
            {"name": "AI / xAI", "name_en": "AI / xAI", "rev": F["seg_ai"]},
        ],
        "ai_op_loss": F["ai_op_loss"],
        "compute_annualized": round(comp_ann, 1),
        "compute_detail": " + ".join(
            f'{c["name"]} ${round(c["monthly_b"] * 12, 1):g}B' for c in F["compute_contracts"]),
        "compute_vs_total": comp_ann / F["revenue_total"],     # 算力合同 / 2025总收入
        "compute_vs_ai": comp_ann / F["seg_ai"],               # 算力合同 / 2025 AI实际收入
        "ai_share": F["seg_ai"] / F["revenue_total"],          # AI 占 2025 收入
        "ps_ratio": (spcx_mcap / (F["revenue_total"] * 1e9)) if spcx_mcap else None,
    }


def fetch_shares() -> dict:
    """各标的流通股数（market_cap / price），用于实时市值估算。
    股数变化慢，由 app 低频刷新（随 history 一起，每 30 分钟）。"""
    shares = {}
    for t in ALL_TICKERS:
        try:
            fi = yf.Ticker(t).fast_info
            mc, lp = fi.market_cap, fi.last_price
            if mc and lp:
                shares[t] = mc / lp
        except Exception:
            pass
        time.sleep(0.1)
    return shares


def fetch_quotes(shares: dict | None = None) -> dict:
    """实时报价：Yahoo v8 chart 端点（延迟约 2-5 秒，免费、无需 API key）。
    比 fast_info 快一个数量级。市值 = 实时价 × 缓存股数。"""
    shares = shares or {}
    out = {}
    for t in ALL_TICKERS:
        try:
            r = _session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{t}",
                params={"interval": "1m", "range": "1d"},
                headers=_YHEADERS, timeout=8)
            m = r.json()["chart"]["result"][0]["meta"]
            last = m.get("regularMarketPrice")
            prev = m.get("chartPreviousClose") or m.get("previousClose")
            chg = (last / prev - 1.0) if (last and prev) else None
            mcap = (last * shares[t]) if (last and shares.get(t)) else None
            out[t] = {"last": last, "prev": prev, "chg": chg, "mcap": mcap,
                      "quote_time": m.get("regularMarketTime")}
        except Exception as e:
            out[t] = {"last": None, "prev": None, "chg": None, "mcap": None,
                      "quote_time": None, "error": f"{type(e).__name__}: {e}"}
        time.sleep(0.05)
    return out


def fetch_history() -> dict:
    """6 个月日线收盘价与成交量，列为 ticker。SPCX 今日上市，可能为空列。
    批量下载偶发单票失败，对缺失列做单票重试。"""
    df = yf.download(ALL_TICKERS, period="6mo", interval="1d",
                     progress=False, auto_adjust=True, group_by="column")
    closes = df["Close"] if "Close" in df else pd.DataFrame()
    volumes = df["Volume"] if "Volume" in df else pd.DataFrame()
    for t in ALL_TICKERS:
        if t == "SPCX":
            continue  # 今日上市，无日线历史属正常
        if t not in closes or closes[t].dropna().empty:
            try:
                time.sleep(0.5)
                h = yf.Ticker(t).history(period="6mo", interval="1d", auto_adjust=True)
                if not h.empty:
                    h.index = h.index.tz_localize(None)
                    closes[t] = h["Close"]
                    volumes[t] = h["Volume"]
            except Exception:
                pass
    return {"close": closes, "volume": volumes, "fetched_at": time.time()}


def _zscore(series: pd.Series, value: float, window: int = Z_WINDOW):
    s = series.dropna().tail(window)
    if len(s) < 10 or s.std() == 0 or value is None:
        return None
    return float((value - s.mean()) / s.std())


def compute(quotes: dict, hist: dict) -> dict:
    closes, volumes = hist["close"], hist["volume"]
    _qt = [q.get("quote_time") for q in quotes.values() if q.get("quote_time")]
    data_delay = round(time.time() - max(_qt)) if _qt else None
    rets = closes.pct_change()
    spy_ret = rets.get("SPY")
    spy_today = (quotes.get("SPY") or {}).get("chg") or 0.0

    # ---- 情绪篮子：β剥离残差 ----
    resid_today_list, resid_hist, betas_list = [], [], []
    for t in GROUPS["basket"]:
        if t not in rets or spy_ret is None:
            continue
        pair = pd.concat([rets[t], spy_ret], axis=1, keys=["r", "m"]).dropna().tail(BETA_WINDOW)
        if len(pair) < 20:
            continue
        beta = pair["r"].cov(pair["m"]) / pair["m"].var() if pair["m"].var() else 1.0
        betas_list.append(beta)
        resid_hist.append(pair["r"] - beta * pair["m"])
        chg = (quotes.get(t) or {}).get("chg")
        if chg is not None:
            resid_today_list.append(chg - beta * spy_today)

    basket_resid_today = (sum(resid_today_list) / len(resid_today_list)) if resid_today_list else None
    basket_resid_series = pd.concat(resid_hist, axis=1).mean(axis=1) if resid_hist else pd.Series(dtype=float)
    z_resid = _zscore(basket_resid_series, basket_resid_today)

    # ---- 模型合理价（独立于 SPCX 实际成交价）----
    # 三因子：IPO 询价锚 × (1 + β_spcx·大盘 + λ·板块超额)
    beta_basket = (sum(betas_list) / len(betas_list)) if betas_list else 1.0
    beta_spcx = min(SPCX_BETA_CAP, 1.0 + (beta_basket - 1.0) * SPCX_BETA_SHRINK)  # 向1收缩并封顶：$2T 巨头波动更近大盘
    fair_resid = basket_resid_today if basket_resid_today is not None else 0.0
    mkt_contrib = beta_spcx * spy_today
    sector_contrib = SECTOR_LAMBDA * fair_resid
    fair_price = SPCX_IPO_PRICE * (1 + mkt_contrib + sector_contrib)

    # ---- DXYZ 溢价 ----
    dxyz_last = (quotes.get("DXYZ") or {}).get("last")
    dxyz_prem = (dxyz_last / DXYZ_NAV - 1.0) if dxyz_last else None
    prem_series = (closes["DXYZ"] / DXYZ_NAV - 1.0) if "DXYZ" in closes else pd.Series(dtype=float)
    z_prem = _zscore(prem_series, dxyz_prem)

    # ---- 关注度：代理基金昨日成交量 vs 20 日均量 ----
    vol_zs = []
    for t in ("DXYZ", "NASA", "XOVR"):
        if t in volumes:
            v = volumes[t].dropna()
            if len(v) > VOL_WINDOW + 2:
                z = _zscore(v.iloc[:-1], float(v.iloc[-1]), VOL_WINDOW)
                if z is not None:
                    vol_zs.append(z)
    z_vol = sum(vol_zs) / len(vol_zs) if vol_zs else None

    # ---- 综合情绪 0-100 ----
    comps = {"premium_z": z_prem, "basket_residual_z": z_resid, "attention_z": z_vol}
    valid = [max(-4.0, min(4.0, z)) for z in comps.values() if z is not None]
    score = round(norm_cdf(sum(valid) / len(valid)) * 100, 1) if valid else None

    # ---- 卡片数据 ----
    spcx = quotes.get("SPCX") or {}
    spcx_last, spcx_mcap = spcx.get("last"), spcx.get("mcap")
    googl_mcap = (quotes.get("GOOGL") or {}).get("mcap")
    stake_value = GOOGL_SPACEX_STAKE * spcx_mcap if spcx_mcap else None

    table = []
    for group, tickers in GROUPS.items():
        for t in tickers:
            q = quotes.get(t) or {}
            exp = EXPOSURES.get(t, {})
            table.append({
                "ticker": t, "name": NAMES.get(t, t), "name_en": NAMES_EN.get(t, t),
                "group": group,
                "last": q.get("last"), "chg": q.get("chg"), "mcap": q.get("mcap"),
                "exposure_pct": exp.get("pct"), "exposure_note": exp.get("note"),
                "exposure_note_en": exp.get("note_en"),
                "exposure_asof": exp.get("asof"), "error": q.get("error"),
                "bind_reason": BIND_REASON.get(t, ""),
                "bind_reason_en": BIND_REASON_EN.get(t, ""),
            })

    return {
        "asof_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_delay_sec": data_delay,
        "spcx": {
            "last": spcx_last, "mcap": spcx_mcap, "ipo_price": SPCX_IPO_PRICE,
            "vs_ipo": (spcx_last / SPCX_IPO_PRICE - 1.0) if spcx_last else None,
        },
        "googl": {
            "stake_pct": GOOGL_SPACEX_STAKE, "stake_value": stake_value,
            "pct_of_googl": (stake_value / googl_mcap) if (stake_value and googl_mcap) else None,
        },
        "dxyz": {"nav": DXYZ_NAV, "nav_asof": DXYZ_NAV_ASOF,
                 "premium": dxyz_prem, "premium_z": z_prem},
        "basket": {"residual_today": basket_resid_today, "residual_z": z_resid},
        "sentiment": {"score": score, "components": comps},
        "fair_value": {
            "price": round(fair_price, 2),
            "anchor": SPCX_IPO_PRICE,
            "beta_spcx": round(beta_spcx, 2),
            "beta_basket": round(beta_basket, 2),
            "lambda": SECTOR_LAMBDA,
            "spy_chg": spy_today,
            "basket_resid": fair_resid,
            "mkt_contrib": mkt_contrib,
            "sector_contrib": sector_contrib,
            "vs_last": ((spcx_last / fair_price - 1.0) if (spcx_last and fair_price) else None),
        },
        "narrative": _narrative(spcx_mcap),
        "table": table,
    }


def _intraday_5m(ticker: str, rng: str = "2d") -> dict:
    """拉单标的 5 分钟 K 线收盘价，返回 {ts: close}。失败返回空。"""
    try:
        r = _session.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "5m", "range": rng}, headers=_YHEADERS, timeout=10)
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        return {ts[i]: closes[i] for i in range(len(ts)) if closes[i] is not None}
    except Exception:
        return {}


def backfill_intraday(hist: dict, rng: str = "2d") -> list:
    """用 5 分钟 K 线回算最近 N 天的情绪序列，口径与 compute 一致
    （β 剥离残差 + DXYZ 溢价，相对日线分布取 z；intraday 无成交量分量）。
    返回 [{ts, spcx, sentiment, dxyz_premium}, ...] 按时间升序。"""
    closes = hist["close"]
    rets = closes.pct_change()
    spy_ret = rets.get("SPY")
    if spy_ret is None:
        return []

    # β 与日线残差分布（参考分布，与 compute 同窗口）
    betas, resid_hist = {}, []
    for t in GROUPS["basket"]:
        if t not in rets:
            continue
        pair = pd.concat([rets[t], spy_ret], axis=1, keys=["r", "m"]).dropna().tail(BETA_WINDOW)
        if len(pair) < 20:
            continue
        beta = pair["r"].cov(pair["m"]) / pair["m"].var() if pair["m"].var() else 1.0
        betas[t] = beta
        resid_hist.append(rets[t] - beta * spy_ret)
    resid_series = (pd.concat(resid_hist, axis=1).mean(axis=1).dropna().tail(Z_WINDOW)
                    if resid_hist else pd.Series(dtype=float))
    resid_mu = resid_series.mean() if len(resid_series) >= 10 else None
    resid_sd = resid_series.std() if len(resid_series) >= 10 else None
    prem_s = ((closes["DXYZ"] / DXYZ_NAV - 1.0).dropna().tail(Z_WINDOW)
              if "DXYZ" in closes else pd.Series(dtype=float))
    prem_mu = prem_s.mean() if len(prem_s) >= 10 else None
    prem_sd = prem_s.std() if len(prem_s) >= 10 else None

    # 拉 intraday 收盘价
    intra = {t: _intraday_5m(t, rng) for t in set(list(betas) + ["SPY", "DXYZ", "SPCX"])}

    # 各交易日昨收（用于日内累计收益剥离隔夜跳空）
    def prevclose(t, ts):
        if t not in closes:
            return None
        daily = closes[t].dropna()
        date = pd.Timestamp(ts, unit="s", tz="UTC").normalize().tz_localize(None)
        prior = daily[daily.index.normalize() < date]
        return float(prior.iloc[-1]) if len(prior) else None

    # 各交易日代理基金成交量 z（attention 分量，与实时口径一致；按日广播到该日所有点）
    volumes = hist["volume"]
    _attn_cache = {}
    def attn_z(ts):
        date = pd.Timestamp(ts, unit="s", tz="UTC").normalize().tz_localize(None)
        if date in _attn_cache:
            return _attn_cache[date]
        zs = []
        for t in ("DXYZ", "NASA", "XOVR"):
            if t in volumes:
                v = volumes[t].dropna()
                v_upto = v[v.index.normalize() <= date]
                if len(v_upto) > VOL_WINDOW + 1:
                    ref = v_upto.iloc[:-1].tail(VOL_WINDOW)
                    if ref.std() > 0:
                        zs.append((float(v_upto.iloc[-1]) - ref.mean()) / ref.std())
        val = sum(zs) / len(zs) if zs else None
        _attn_cache[date] = val
        return val

    out = []
    for ts in sorted(intra.get("SPY", {})):
        spy_p, spy_pc = intra["SPY"].get(ts), prevclose("SPY", ts)
        if not spy_p or not spy_pc:
            continue
        spy_r = spy_p / spy_pc - 1.0
        resids = []
        for t, b in betas.items():
            p, pc = intra.get(t, {}).get(ts), prevclose(t, ts)
            if p and pc:
                resids.append((p / pc - 1.0) - b * spy_r)
        resid_t = sum(resids) / len(resids) if resids else None
        z_resid = ((resid_t - resid_mu) / resid_sd
                   if (resid_t is not None and resid_mu is not None and resid_sd) else None)
        dp = intra.get("DXYZ", {}).get(ts)
        prem_t = (dp / DXYZ_NAV - 1.0) if dp else None
        z_prem = ((prem_t - prem_mu) / prem_sd
                  if (prem_t is not None and prem_mu is not None and prem_sd) else None)
        z_attn = attn_z(ts)
        zs = [max(-4.0, min(4.0, z)) for z in (z_prem, z_resid, z_attn) if z is not None]
        score = round(norm_cdf(sum(zs) / len(zs)) * 100, 1) if zs else None
        out.append({"ts": ts, "spcx": intra.get("SPCX", {}).get(ts),
                    "sentiment": score, "dxyz_premium": prem_t})
    return out
