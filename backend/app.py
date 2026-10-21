"""量化系统主应用"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from database import init_db
from routers import positions, market, strategies, trades, data, ai_assistant, paper_trade, watchlist, sequoia
from config import HOST, PORT

app = FastAPI(
    title="量维 QuantFlow",
    description="个人量化交易平台",
    version="1.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(positions.router)
app.include_router(market.router)
app.include_router(strategies.router)
app.include_router(trades.router)
app.include_router(data.router)
app.include_router(ai_assistant.router)
app.include_router(paper_trade.router)
app.include_router(watchlist.router)
app.include_router(sequoia.router)

# 静态文件
frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
def serve_index():
    """首页"""
    return FileResponse(frontend_dir / "index.html")


@app.get("/positions")
def serve_positions():
    return FileResponse(frontend_dir / "positions.html")


@app.get("/market")
def serve_market():
    return FileResponse(frontend_dir / "market.html")


@app.get("/strategies")
def serve_strategies():
    return FileResponse(frontend_dir / "strategies.html")


@app.get("/trades")
def serve_trades():
    return FileResponse(frontend_dir / "trades.html")


@app.get("/data")
def serve_data():
    return FileResponse(frontend_dir / "data.html")


@app.get("/paper")
def serve_paper():
    return FileResponse(frontend_dir / "paper.html")


@app.get("/scan")
def serve_scan():
    return FileResponse(frontend_dir / "scan.html")


@app.get("/watchlist")
def serve_watchlist():
    return FileResponse(frontend_dir / "watchlist.html")


@app.get("/sequoia")
def serve_sequoia():
    return FileResponse(frontend_dir / "sequoia.html")


@app.get("/stock/{symbol}")
def serve_stock_detail(symbol: str):
    return FileResponse(frontend_dir / "stock_detail.html")


@app.on_event("startup")
def startup():
    """启动时初始化数据库"""
    init_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=True)
