"""策略服务（含真实数据回测）"""
from typing import List, Dict, Optional
from datetime import date
import logging
import math

logger = logging.getLogger(__name__)


class StrategyService:
    """策略管理服务"""

    # 预定义策略模板
    STRATEGY_TEMPLATES = {
        "momentum": {
            "name": "均线动量突破",
            "description": "基于均线系统的趋势跟踪策略",
            "parameters": {
                "fast_period": 5,
                "slow_period": 20,
                "volume_threshold": 1.5,
            },
        },
        "mean_reversion": {
            "name": "均值回归",
            "description": "基于布林带的均值回归策略",
            "parameters": {
                "period": 20,
                "num_std": 2.0,
            },
        },
        "multi_factor": {
            "name": "多因子选股",
            "description": "基于多因子模型的选股策略",
            "parameters": {
                "factors": ["pe", "pb", "roe", "momentum"],
                "weights": [0.3, 0.2, 0.3, 0.2],
                "top_n": 10,
            },
        },
        "grid": {
            "name": "网格交易",
            "description": "基于价格区间的网格交易策略",
            "parameters": {
                "upper_price": 100,
                "lower_price": 80,
                "grid_count": 10,
                "amount_per_grid": 10000,
            },
        },
        "event_driven": {
            "name": "事件驱动",
            "description": "基于特定事件触发的交易策略",
            "parameters": {
                "event_types": ["earnings", "dividend", "news"],
                "threshold": 0.05,
            },
        },
        "multi_period_bullish": {
            "name": "日线多周期多头",
            "description": "日/周/月线均线多头排列的趋势跟踪策略，MA5>MA10>MA20>MA60 时持有",
            "parameters": {
                "ma_periods": [5, 10, 20, 60],
                "timeframes": ["daily", "weekly", "monthly"],
                "top_n": 10,
            },
        },
    }

    def get_templates(self) -> List[Dict]:
        """获取策略模板列表"""
        return [
            {"type": k, **v}
            for k, v in self.STRATEGY_TEMPLATES.items()
        ]

    def get_template(self, strategy_type: str) -> Optional[Dict]:
        """获取指定策略模板"""
        return self.STRATEGY_TEMPLATES.get(strategy_type)

    def backtest(
        self,
        strategy_type: str,
        symbol: str,
        parameters: Dict,
        start_date: str,
        end_date: str,
    ) -> Dict:
        """基于真实K线数据的回测引擎

        流程：取真实日线 -> 按策略类型生成信号 -> 逐日模拟成交 -> 计算真实指标。
        数据来源走 data_service（数据源路由 + 标准化），不再是硬编码结果。
        """
        from services.data_service import data_service

        klines = data_service.get_kline_data(symbol, "daily", start_date, end_date)
        if not klines or len(klines) < 2:
            raise ValueError(f"无法获取 {symbol} 的K线数据，回测中止")

        closes = [k["close"] for k in klines]
        dates = [k["date"] for k in klines]
        params = parameters or {}

        # 1) 生成每日持仓信号（0=空仓, 1=满仓）
        position = self._build_positions(strategy_type, closes, params)

        # 2) 逐日模拟：满仓时按收盘价持有，次日按收盘价调仓
        cash = 1.0
        shares = 0.0
        equity_curve = []
        trades = []          # 记录每笔买入/卖出
        entry_price = None
        for i in range(len(closes)):
            price = closes[i]
            target = position[i]
            if target == 1 and shares == 0:
                shares = cash / price
                cash = 0.0
                entry_price = price
                trades.append({"date": dates[i], "action": "buy", "price": price})
            elif target == 0 and shares > 0:
                cash = shares * price
                shares = 0.0
                trades.append({"date": dates[i], "action": "sell", "price": price,
                               "return": (price / entry_price - 1) if entry_price else 0})
                entry_price = None
            equity_curve.append(cash + shares * price)

        # 收盘平仓
        if shares > 0:
            cash = shares * closes[-1]
            trades.append({"date": dates[-1], "action": "sell", "price": closes[-1],
                           "return": (closes[-1] / entry_price - 1) if entry_price else 0})
            equity_curve[-1] = cash

        # 3) 计算真实指标
        total_return = equity_curve[-1] - 1.0
        n_days = len(closes)
        annual_return = (equity_curve[-1]) ** (252 / n_days) - 1 if equity_curve[-1] > 0 else -1.0

        # 最大回撤
        peak = equity_curve[0]
        max_dd = 0.0
        for v in equity_curve:
            peak = max(peak, v)
            if peak > 0:
                max_dd = min(max_dd, v / peak - 1)

        # 夏普（日收益年化，无风险利率 0）
        rets = [equity_curve[i] / equity_curve[i - 1] - 1 for i in range(1, len(equity_curve))]
        if len(rets) > 1:
            mean_r = sum(rets) / len(rets)
            var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
            std = math.sqrt(var) if var > 0 else 0.0
            sharpe = (mean_r / std * math.sqrt(252)) if std > 0 else 0.0
        else:
            sharpe = 0.0

        # 胜率（按平仓交易）
        closed = [t for t in trades if t["action"] == "sell" and "return" in t]
        wins = [t for t in closed if t["return"] > 0]
        win_rate = (len(wins) / len(closed)) if closed else 0.0

        return {
            "symbol": symbol,
            "start_date": dates[0],
            "end_date": dates[-1],
            "total_return": round(total_return, 4),
            "annual_return": round(annual_return, 4),
            "max_drawdown": round(max_dd, 4),
            "sharpe_ratio": round(sharpe, 4),
            "win_rate": round(win_rate, 4),
            "total_trades": len(closed),
            "profit_trades": len(wins),
            "data_points": n_days,
            "data_source": "real",
        }

    def _build_positions(self, strategy_type: str, closes: List[float], params: Dict) -> List[int]:
        """按策略类型生成每日目标仓位序列（0/1）"""
        n = len(closes)
        pos = [0] * n

        def ma(series, i, window):
            if i + 1 < window:
                return None
            return sum(series[i + 1 - window:i + 1]) / window

        if strategy_type == "momentum":
            fast = int(params.get("fast_period", 5))
            slow = int(params.get("slow_period", 20))
            for i in range(n):
                mf, ms = ma(closes, i, fast), ma(closes, i, slow)
                if mf is not None and ms is not None:
                    pos[i] = 1 if mf > ms else 0
        elif strategy_type == "mean_reversion":
            period = int(params.get("period", 20))
            num_std = float(params.get("num_std", 2.0))
            holding = 0
            for i in range(n):
                m = ma(closes, i, period)
                if m is None:
                    pos[i] = holding
                    continue
                window = closes[i + 1 - period:i + 1]
                std = math.sqrt(sum((c - m) ** 2 for c in window) / period)
                if closes[i] < m - num_std * std:
                    holding = 1
                elif closes[i] > m + num_std * std:
                    holding = 0
                pos[i] = holding
        elif strategy_type == "grid":
            lower = float(params.get("lower_price", min(closes)))
            upper = float(params.get("upper_price", max(closes)))
            mid = (upper + lower) / 2
            for i in range(n):
                pos[i] = 1 if closes[i] < mid else 0
        elif strategy_type == "multi_factor":
            # 无因子数据时退化为动量（仅用价格，保证是真数据计算）
            for i in range(n):
                m20, m60 = ma(closes, i, 20), ma(closes, i, 60)
                if m20 is not None and m60 is not None:
                    pos[i] = 1 if m20 > m60 else 0
        elif strategy_type == "multi_period_bullish":
            # 日线多头排列（MA5>MA10>MA20>MA60）时持有，否则空仓
            for i in range(n):
                m5, m10, m20, m60 = ma(closes, i, 5), ma(closes, i, 10), ma(closes, i, 20), ma(closes, i, 60)
                if None not in (m5, m10, m20, m60):
                    pos[i] = 1 if m5 > m10 > m20 > m60 else 0
        else:
            # 默认：买入持有
            pos = [1] * n

        return pos


strategy_service = StrategyService()
