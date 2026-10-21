"""配置文件"""
import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# 数据库配置
DATABASE_URL = f"sqlite:///{BASE_DIR}/data/quant.db"

# 数据源配置
DATA_SOURCE = "baostock"  # 可选: akshare, tushare, baostock

# Tushare token (如果使用 tushare)
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "a22e1c5c7a710f5127808e559010a2c4b2136b08251ea432e28803bd")

# 服务器配置
HOST = "0.0.0.0"
PORT = 8000

# Vibe-Trading 集成配置（AI 策略助手）
VIBE_CLI = os.getenv("VIBE_CLI", "/Users/qiaogang/.venvs/vibe-trading/bin/vibe-trading")
VIBE_WORK_DIR = os.getenv("VIBE_WORK_DIR", "/Users/qiaogang/traeworkspace/lianghua/vibe-trading")
VIBE_RUNS_DIR = os.getenv(
    "VIBE_RUNS_DIR", str(Path.home() / ".vibe-trading" / "runs")
)
VIBE_TIMEOUT = int(os.getenv("VIBE_TIMEOUT", "1200"))  # 单次回测超时（秒）

# TradingAgents 集成配置（AI 投资报告）
TRADINGAGENTS_PYTHON = os.getenv(
    "TRADINGAGENTS_PYTHON",
    "/Users/qiaogang/traeworkspace/lianghua/TradingAgents-main/.venv/bin/python",
)
TRADINGAGENTS_DIR = os.getenv(
    "TRADINGAGENTS_DIR",
    "/Users/qiaogang/traeworkspace/lianghua/TradingAgents-main",
)
TRADINGAGENTS_SCRIPT = os.getenv(
    "TRADINGAGENTS_SCRIPT",
    str(Path(__file__).parent / "services" / "tradingagents_report.py"),
)
TRADINGAGENTS_REPORTS_DIR = os.getenv(
    "TRADINGAGENTS_REPORTS_DIR",
    str(Path.home() / ".tradingagents" / "logs" / "reports"),
)
# 单只股票生成报告的超时（秒）
TRADINGAGENTS_TIMEOUT = int(os.getenv("TRADINGAGENTS_TIMEOUT", "1800"))

# Sequoia-X 集成配置（A股选股系统，独立菜单）
SEQUOIA_DIR = os.getenv("SEQUOIA_DIR", "/Users/qiaogang/traeworkspace/lianghua/Sequoia-X")
SEQUOIA_PYTHON = os.getenv("SEQUOIA_PYTHON", f"{SEQUOIA_DIR}/.venv/bin/python")
SEQUOIA_RUN_TIMEOUT = int(os.getenv("SEQUOIA_RUN_TIMEOUT", "900"))  # 选股子进程超时（秒）

# 市场配置
MARKETS = {
    "A股": {
        "exchange": ["SSE", "SZSE"],
        "trading_hours": "09:30-11:30,13:00-15:00",
    },
    "港股": {
        "exchange": ["HKEX"],
        "trading_hours": "09:30-12:00,13:00-16:00",
    },
    "美股": {
        "exchange": ["NYSE", "NASDAQ"],
        "trading_hours": "09:30-16:00",
    },
}
