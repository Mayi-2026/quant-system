"""模拟交易服务：信号生成 + 订单执行 + 持仓管理"""
import json
import logging
from typing import List, Dict, Optional
from datetime import datetime

from sqlalchemy.orm import Session
from models.paper_trade import PaperAccount, PaperPosition, PaperOrder, PaperTradeLog
from models.strategy import Strategy
from services.data_service import data_service

logger = logging.getLogger(__name__)


class PaperTradeService:
    """模拟交易引擎"""

    # ========== 账户管理 ==========

    def create_account(self, db: Session, name: str, initial_capital: float = 1000000.0) -> PaperAccount:
        account = PaperAccount(name=name, initial_capital=initial_capital, current_cash=initial_capital)
        db.add(account)
        db.commit()
        db.refresh(account)
        self._log(db, account.id, "signal", "info", f"模拟账户创建: {name}, 初始资金 {initial_capital:,.0f}")
        return account

    def get_accounts(self, db: Session) -> List[PaperAccount]:
        return db.query(PaperAccount).all()

    def get_account(self, db: Session, account_id: int) -> Optional[PaperAccount]:
        return db.query(PaperAccount).filter(PaperAccount.id == account_id).first()

    def get_account_summary(self, db: Session, account_id: int) -> Dict:
        account = self.get_account(db, account_id)
        if not account:
            return {}
        positions = self.get_positions(db, account_id)
        position_value = sum(p.quantity * p.current_price for p in positions)
        total = account.current_cash + position_value
        return {
            "id": account.id,
            "name": account.name,
            "status": account.status,
            "initial_capital": account.initial_capital,
            "current_cash": account.current_cash,
            "position_value": position_value,
            "total_value": total,
            "total_return": total - account.initial_capital,
            "total_return_pct": (total - account.initial_capital) / account.initial_capital * 100,
            "position_count": len(positions),
        }

    # ========== 持仓管理 ==========

    def get_positions(self, db: Session, account_id: int) -> List[PaperPosition]:
        return db.query(PaperPosition).filter(
            PaperPosition.account_id == account_id,
            PaperPosition.quantity > 0,
        ).all()

    def get_or_create_position(self, db: Session, account_id: int, symbol: str, name: str = None) -> PaperPosition:
        pos = db.query(PaperPosition).filter(
            PaperPosition.account_id == account_id,
            PaperPosition.symbol == symbol,
        ).first()
        if not pos:
            pos = PaperPosition(account_id=account_id, symbol=symbol, name=name)
            db.add(pos)
            db.commit()
            db.refresh(pos)
        return pos

    def update_position_prices(self, db: Session, account_id: int):
        """更新持仓的最新价格"""
        positions = self.get_positions(db, account_id)
        for pos in positions:
            try:
                quote = data_service.get_realtime_quote(pos.symbol)
                if quote and quote.get("price"):
                    pos.current_price = quote["price"]
                    if not pos.name:
                        pos.name = quote.get("name", "")
            except Exception as e:
                logger.warning(f"更新持仓价格失败 {pos.symbol}: {e}")
        db.commit()

    # ========== 订单执行 ==========

    def place_order(
        self,
        db: Session,
        account_id: int,
        symbol: str,
        direction: str,
        quantity: int,
        order_type: str = "market",
        price: float = None,
        strategy_id: int = None,
        reason: str = None,
    ) -> PaperOrder:
        """下单（模拟执行）"""
        account = self.get_account(db, account_id)
        if not account or account.status != "active":
            raise ValueError("账户不存在或未激活")

        # 获取当前价格
        quote = data_service.get_realtime_quote(symbol)
        current_price = price
        if not current_price:
            if quote and quote.get("price"):
                current_price = quote["price"]
            else:
                raise ValueError(f"无法获取 {symbol} 的实时价格")

        name = quote.get("name", symbol) if quote else symbol

        # 创建订单
        order = PaperOrder(
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            name=name,
            direction=direction,
            order_type=order_type,
            price=current_price,
            quantity=quantity,
            status="pending",
            signal_reason=reason,
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        self._log(db, account_id, "order", "info",
                  f"委托{('买入' if direction == 'buy' else '卖出')} {name}({symbol}) {quantity}股 @ {current_price:.2f}")

        # 模拟立即成交
        return self._fill_order(db, order, current_price)

    def _fill_order(self, db: Session, order: PaperOrder, fill_price: float) -> PaperOrder:
        """成交订单"""
        account = self.get_account(db, order.account_id)
        cost = fill_price * order.quantity

        # 计算手续费
        commission = max(cost * 0.0003, 5.0)  # 万三，最低5元
        stamp_tax = cost * 0.001 if order.direction == "sell" else 0  # 卖出千一印花税
        transfer_fee = cost * 0.00002  # 过户费万0.2
        total_fee = commission + stamp_tax + transfer_fee

        if order.direction == "buy":
            total_cost = cost + total_fee
            if total_cost > account.current_cash:
                order.status = "rejected"
                self._log(db, order.account_id, "error", "error",
                          f"资金不足: 需要 {total_cost:,.2f}, 可用 {account.current_cash:,.2f}")
                db.commit()
                return order

            # 扣除资金
            account.current_cash -= total_cost

            # 更新持仓
            pos = self.get_or_create_position(db, order.account_id, order.symbol, order.name)
            total_qty = pos.quantity + order.quantity
            pos.avg_cost = (pos.avg_cost * pos.quantity + fill_price * order.quantity) / total_qty
            pos.quantity = total_qty
            pos.current_price = fill_price

        else:  # sell
            pos = db.query(PaperPosition).filter(
                PaperPosition.account_id == order.account_id,
                PaperPosition.symbol == order.symbol,
            ).first()
            if not pos or pos.quantity < order.quantity:
                order.status = "rejected"
                avail = pos.quantity if pos else 0
                self._log(db, order.account_id, "error", "error",
                          f"持仓不足: 需要 {order.quantity}, 可用 {avail}")
                db.commit()
                return order

            # 增加资金
            account.current_cash += cost - total_fee

            # 更新持仓
            pos.quantity -= order.quantity

        # 标记成交
        order.filled_price = fill_price
        order.filled_quantity = order.quantity
        order.status = "filled"
        order.filled_at = datetime.now()

        self._log(db, order.account_id, "fill", "info",
                  f"成交{('买入' if order.direction == 'buy' else '卖出')} {order.name}({order.symbol}) "
                  f"{order.filled_quantity}股 @ {fill_price:.2f}, 手续费 {total_fee:.2f}")
        db.commit()
        return order

    def cancel_order(self, db: Session, order_id: int) -> Optional[PaperOrder]:
        order = db.query(PaperOrder).filter(PaperOrder.id == order_id).first()
        if not order:
            return None
        if order.status != "pending":
            return order
        order.status = "cancelled"
        db.commit()
        return order

    # ========== 订单查询 ==========

    def get_orders(self, db: Session, account_id: int, limit: int = 50) -> List[PaperOrder]:
        return db.query(PaperOrder).filter(
            PaperOrder.account_id == account_id
        ).order_by(PaperOrder.created_at.desc()).limit(limit).all()

    def get_order(self, db: Session, order_id: int) -> Optional[PaperOrder]:
        return db.query(PaperOrder).filter(PaperOrder.id == order_id).first()

    # ========== 信号生成 ==========

    def generate_signals(self, db: Session, account_id: int, strategy_id: int = None) -> List[Dict]:
        """根据运行中的策略生成交易信号"""
        strategies = db.query(Strategy).filter(Strategy.status == "active")
        if strategy_id:
            strategies = strategies.filter(Strategy.id == strategy_id)
        strategies = strategies.all()

        signals = []
        for s in strategies:
            try:
                sigs = self._evaluate_strategy(s)
                signals.extend(sigs)
            except Exception as e:
                logger.error(f"策略 {s.name} 信号生成失败: {e}")
                self._log(db, account_id, "error", "error", f"策略 {s.name} 信号生成失败: {e}")

        return signals

    def _evaluate_strategy(self, strategy: Strategy) -> List[Dict]:
        """评估单个策略，生成信号"""
        params = strategy.parameters or {}
        stype = strategy.strategy_type
        signals = []

        # 获取K线数据
        try:
            klines = data_service.get_kline_data("000300.SH", "daily")
            if not klines or len(klines) < 30:
                return signals
        except Exception:
            return signals

        closes = [k["close"] for k in klines]

        if stype == "momentum":
            fast = params.get("fast_period", 5)
            slow = params.get("slow_period", 20)
            if len(closes) >= slow:
                ma_fast = sum(closes[-fast:]) / fast
                ma_slow = sum(closes[-slow:]) / slow
                if ma_fast > ma_slow * 1.01:
                    signals.append({
                        "strategy_id": strategy.id,
                        "strategy_name": strategy.name,
                        "symbol": "000300.SH",
                        "direction": "buy",
                        "signal_strength": min((ma_fast - ma_slow) / ma_slow * 100, 10),
                        "reason": f"MA{fast}({ma_fast:.2f}) > MA{slow}({ma_slow:.2f}), 金叉信号",
                    })
                elif ma_fast < ma_slow * 0.99:
                    signals.append({
                        "strategy_id": strategy.id,
                        "strategy_name": strategy.name,
                        "symbol": "000300.SH",
                        "direction": "sell",
                        "signal_strength": min((ma_slow - ma_fast) / ma_slow * 100, 10),
                        "reason": f"MA{fast}({ma_fast:.2f}) < MA{slow}({ma_slow:.2f}), 死叉信号",
                    })

        elif stype == "mean_reversion":
            period = params.get("period", 20)
            num_std = params.get("num_std", 2.0)
            if len(closes) >= period:
                ma = sum(closes[-period:]) / period
                std = (sum((c - ma) ** 2 for c in closes[-period:]) / period) ** 0.5
                current = closes[-1]
                upper = ma + num_std * std
                lower = ma - num_std * std
                if current < lower:
                    signals.append({
                        "strategy_id": strategy.id,
                        "strategy_name": strategy.name,
                        "symbol": "000300.SH",
                        "direction": "buy",
                        "signal_strength": min((lower - current) / std * 5, 10),
                        "reason": f"价格 {current:.2f} < 下轨 {lower:.2f}, 超卖信号",
                    })
                elif current > upper:
                    signals.append({
                        "strategy_id": strategy.id,
                        "strategy_name": strategy.name,
                        "symbol": "000300.SH",
                        "direction": "sell",
                        "signal_strength": min((current - upper) / std * 5, 10),
                        "reason": f"价格 {current:.2f} > 上轨 {upper:.2f}, 超买信号",
                    })

        elif stype == "grid":
            upper = params.get("upper_price", 100)
            lower = params.get("lower_price", 80)
            grid_count = params.get("grid_count", 10)
            current = closes[-1]
            grid_step = (upper - lower) / grid_count
            if grid_step > 0 and lower <= current <= upper:
                grid_pos = (current - lower) / (upper - lower)
                if grid_pos < 0.3:
                    signals.append({
                        "strategy_id": strategy.id,
                        "strategy_name": strategy.name,
                        "symbol": "000300.SH",
                        "direction": "buy",
                        "signal_strength": (0.3 - grid_pos) * 10,
                        "reason": f"价格 {current:.2f} 处于网格低位, 买入信号",
                    })
                elif grid_pos > 0.7:
                    signals.append({
                        "strategy_id": strategy.id,
                        "strategy_name": strategy.name,
                        "symbol": "000300.SH",
                        "direction": "sell",
                        "signal_strength": (grid_pos - 0.7) * 10,
                        "reason": f"价格 {current:.2f} 处于网格高位, 卖出信号",
                    })

        return signals

    def execute_signals(self, db: Session, account_id: int, signals: List[Dict]) -> List[PaperOrder]:
        """执行信号，生成订单"""
        orders = []
        for sig in signals:
            try:
                # 根据信号强度计算下单数量（简单按资金比例）
                account = self.get_account(db, account_id)
                if not account:
                    continue
                alloc = account.current_cash * (sig["signal_strength"] / 100)
                quote = data_service.get_realtime_quote(sig["symbol"])
                if not quote or not quote.get("price"):
                    continue
                price = quote["price"]
                quantity = max(int(alloc / price / 100) * 100, 100)  # 整百手

                order = self.place_order(
                    db, account_id, sig["symbol"], sig["direction"],
                    quantity, strategy_id=sig.get("strategy_id"),
                    reason=sig.get("reason"),
                )
                orders.append(order)
            except Exception as e:
                logger.error(f"执行信号失败: {e}")
                self._log(db, account_id, "error", "error", f"执行信号失败: {e}")
        return orders

    # ========== 日志 ==========

    def get_logs(self, db: Session, account_id: int, limit: int = 100) -> List[PaperTradeLog]:
        return db.query(PaperTradeLog).filter(
            PaperTradeLog.account_id == account_id
        ).order_by(PaperTradeLog.created_at.desc()).limit(limit).all()

    def _log(self, db: Session, account_id: int, event_type: str, level: str, message: str, data: dict = None):
        log = PaperTradeLog(
            account_id=account_id,
            event_type=event_type,
            level=level,
            message=message,
            data=json.dumps(data, ensure_ascii=False) if data else None,
        )
        db.add(log)
        db.commit()


paper_trade_service = PaperTradeService()
