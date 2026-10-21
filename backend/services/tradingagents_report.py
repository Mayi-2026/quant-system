"""TradingAgents 单只股票报告生成脚本。

由 quant-system 通过 TradingAgents 自带 venv 的 python 调用（subprocess），
避免与量化系统后端的 Python 环境冲突。脚本在 TradingAgents 项目目录下运行，
``import tradingagents`` 时会自动向上查找并加载 ``.env``（LLM 端点/Key 等）。

用法：
    .venv/bin/python tradingagents_report.py --ticker 600570.SH --out /tmp/xxx.json

结果以 JSON 写入 --out 指定的文件；脚本自身 stdout 仅打印一行结果，stderr 留给日志。
"""
import argparse
import json
import sys
import traceback
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True, help="原始代码，如 600570.SH")
    parser.add_argument("--date", default=None, help="分析日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--out", required=True, help="结果 JSON 输出路径")
    args = parser.parse_args()

    result: dict = {"ticker": args.ticker}
    try:
        # 延迟导入：确保在 TradingAgents 的 venv 环境里运行
        from tradingagents.agents.rating import run_rating
        from tradingagents.dataflows.date_window import get_current_date
        from tradingagents.dataflows.symbols import normalize_symbol
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        ticker = normalize_symbol(args.ticker)
        trade_date = args.date or get_current_date()

        config = DEFAULT_CONFIG.copy()
        graph = TradingAgentsGraph(debug=False, config=config)
        final_state, signal = graph.propagate(ticker, trade_date)

        save_path = graph.default_report_path(ticker)
        report_md = graph.save_reports(final_state, ticker, save_path=save_path, html=True)
        report_html = save_path / "complete_report.html"

        result.update({
            "ticker_normalized": ticker,
            "trade_date": trade_date,
            "rating": signal or run_rating(final_state),
            "decision": final_state.get("final_trade_decision", ""),
            "report_dir": str(save_path),
            "report_md": str(report_md),
            "report_html": str(report_html) if report_html.exists() else None,
        })
    except Exception as exc:  # noqa: BLE001 — 任何失败都写回结果文件并返回非零码
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            Path(args.out).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        sys.stderr.write(traceback.format_exc())
        sys.exit(1)

    try:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"failed to write output: {exc}\n")
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()