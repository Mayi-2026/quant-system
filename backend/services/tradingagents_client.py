"""TradingAgents 集成客户端

通过 subprocess 调用 TradingAgents 自带 venv 的 python，运行
``services/tradingagents_report.py`` 为单只股票生成 AI 投资报告，
并解析返回的评级与报告路径。
"""
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from config import (
    TRADINGAGENTS_DIR,
    TRADINGAGENTS_PYTHON,
    TRADINGAGENTS_SCRIPT,
    TRADINGAGENTS_TIMEOUT,
)

logger = logging.getLogger(__name__)


class TradingAgentsError(Exception):
    """TradingAgents 调用失败"""


def generate_report(symbol: str, date: Optional[str] = None, timeout: int = TRADINGAGENTS_TIMEOUT) -> dict:
    """同步执行一次 TradingAgents 报告生成（阻塞，供后台线程调用）。

    返回意见：
        {
          "ticker", "ticker_normalized", "trade_date", "rating",
          "decision", "report_dir", "report_md", "report_html",
        }

    失败时抛出 TradingAgentsError。
    """
    fd, out_path = tempfile.mkstemp(suffix=".json", prefix="tradingagents_")
    os.close(fd)
    try:
        cmd = [TRADINGAGENTS_PYTHON, TRADINGAGENTS_SCRIPT, "--ticker", symbol, "--out", out_path]
        if date:
            cmd += ["--date", date]

        proc = subprocess.run(
            cmd,
            cwd=TRADINGAGENTS_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        result: dict = {}
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            try:
                result = json.loads(Path(out_path).read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise TradingAgentsError(f"无法解析 TradingAgents 输出: {exc}")

        if result.get("error"):
            raise TradingAgentsError(result["error"])
        if not result.get("rating") or not result.get("report_html"):
            stderr = (proc.stderr or "")[-800:]
            raise TradingAgentsError(f"报告生成失败（returncode={proc.returncode}）。stderr: {stderr}")

        return result
    except subprocess.TimeoutExpired:
        raise TradingAgentsError(f"报告生成超时（>{timeout}s）")
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


tradingagents_client = type("TradingAgentsClient", (), {
    "generate_report": staticmethod(generate_report),
})()