/**
 * API 封装
 */
const API_BASE = '';

const api = {
  // 通用请求
  async request(url, options = {}) {
    try {
      const res = await fetch(API_BASE + url, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
      });
      if (!res.ok) {
        // 尝试解析后端返回的错误信息
        let errorMsg = `HTTP ${res.status}`;
        try {
          const errorData = await res.json();
          if (errorData.detail) {
            errorMsg = errorData.detail;
          }
        } catch (e) {
          // 忽略解析错误
        }
        throw new Error(errorMsg);
      }
      return await res.json();
    } catch (e) {
      console.error('API Error:', url, e);
      throw e;
    }
  },

  // 仪表盘
  getDashboard(accountId = 1) {
    return this.request(`/api/data/dashboard?account_id=${accountId}`);
  },
  // 收益日历（日/月/年收益金额）
  getPnlCalendar(accountId = 1, years = 5) {
    return this.request(`/api/data/pnl-calendar?account_id=${accountId}&years=${years}`);
  },

  // 持仓
  getPositions(accountId = 1) {
    return this.request(`/api/positions/?account_id=${accountId}`);
  },
  createPosition(data) {
    return this.request('/api/positions/', { method: 'POST', body: JSON.stringify(data) });
  },
  deletePosition(id) {
    return this.request(`/api/positions/${id}`, { method: 'DELETE' });
  },

  // 行情
  getQuote(symbol) {
    return this.request(`/api/market/quote/${symbol}`);
  },
  getKline(symbol, period = 'daily') {
    return this.request(`/api/market/kline/${symbol}?period=${period}`);
  },
  getIndices() {
    return this.request('/api/market/indices');
  },
  searchStock(keyword) {
    return this.request(`/api/market/search?keyword=${encodeURIComponent(keyword)}`);
  },
  getSectors() {
    return this.request('/api/market/sectors');
  },
  getSectorOverview() {
    return this.request('/api/market/sector-overview');
  },
  getUsOverview() {
    return this.request('/api/market/us-overview');
  },
  scanStocks(condition) {
    return this.request(`/api/market/scan?condition=${encodeURIComponent(condition)}`);
  },
  getMarketBreadth() {
    return this.request('/api/market/breadth');
  },
  getTechnical(symbol, period = 'daily') {
    return this.request(`/api/market/technical/${symbol}?period=${period}`);
  },

  // 自选股
  getWatchlist() {
    return this.request('/api/watchlist/');
  },
  addToWatchlist(data) {
    return this.request('/api/watchlist/', { method: 'POST', body: JSON.stringify(data) });
  },
  removeFromWatchlist(id) {
    return this.request(`/api/watchlist/${id}`, { method: 'DELETE' });
  },
  updateWatchlistItem(id, data) {
    return this.request(`/api/watchlist/${id}`, { method: 'PUT', body: JSON.stringify(data) });
  },
  generateWatchlistReports() {
    return this.request('/api/watchlist/generate-reports', { method: 'POST' });
  },

  // Sequoia 选股
  getSequoiaStatus() {
    return this.request('/api/sequoia/status');
  },
  syncSequoia(mode = 'recent', days = 30) {
    return this.request(`/api/sequoia/sync?mode=${mode}&days=${days}`, { method: 'POST' });
  },
  runSequoia() {
    return this.request('/api/sequoia/run', { method: 'POST' });
  },
  getSequoiaRunStatus() {
    return this.request('/api/sequoia/run/status');
  },

  // 策略
  getStrategies() {
    return this.request('/api/strategies/');
  },
  getStrategyTemplates() {
    return this.request('/api/strategies/templates');
  },
  createStrategy(data) {
    return this.request('/api/strategies/', { method: 'POST', body: JSON.stringify(data) });
  },
  updateStrategyStatus(id, status) {
    return this.request(`/api/strategies/${id}/status?status=${status}`, { method: 'PUT' });
  },

  // 策略回测（Vibe-Trading 引擎，后台任务）
  runStrategyBacktest(id, params) {
    return this.request(`/api/strategies/${id}/backtest`, { method: 'POST', body: JSON.stringify(params) });
  },
  getBacktestDetail(backtestId) {
    return this.request(`/api/strategies/backtest/${backtestId}`);
  },

  // AI 助手
  aiChat(sessionId, message) {
    return this.request('/api/ai/chat', { method: 'POST', body: JSON.stringify({ session_id: sessionId, message }) });
  },
  getAiTask(taskId) {
    return this.request(`/api/ai/tasks/${taskId}`);
  },
  getAiSessions() {
    return this.request('/api/ai/sessions');
  },
  getAiSession(id) {
    return this.request(`/api/ai/sessions/${id}`);
  },

  // 交易
  getTrades(accountId = 1) {
    return this.request(`/api/trades/?account_id=${accountId}`);
  },
  createTrade(data) {
    return this.request('/api/trades/', { method: 'POST', body: JSON.stringify(data) });
  },
  getTradeSummary(accountId = 1) {
    return this.request(`/api/trades/summary?account_id=${accountId}`);
  },

  // 数据同步
  syncQuotes() {
    return this.request('/api/data/sync/quotes', { method: 'POST' });
  },
  syncKline(symbol, days = 1825) {
    return this.request(`/api/data/sync/kline/${symbol}?days=${days}`, { method: 'POST' });
  },
  getDataStats() {
    return this.request('/api/data/stats');
  },

  // 模拟交易
  getPaperAccounts() {
    return this.request('/api/paper/accounts');
  },
  createPaperAccount(data) {
    return this.request('/api/paper/accounts', { method: 'POST', body: JSON.stringify(data) });
  },
  getPaperAccount(id) {
    return this.request(`/api/paper/accounts/${id}`);
  },
  getPaperPositions(id) {
    return this.request(`/api/paper/accounts/${id}/positions`);
  },
  placePaperOrder(id, data) {
    return this.request(`/api/paper/accounts/${id}/orders`, { method: 'POST', body: JSON.stringify(data) });
  },
  getPaperOrders(id, limit = 50) {
    return this.request(`/api/paper/accounts/${id}/orders?limit=${limit}`);
  },
  cancelPaperOrder(orderId) {
    return this.request(`/api/paper/orders/${orderId}/cancel`, { method: 'POST' });
  },
  generatePaperSignals(id, strategyId) {
    var url = `/api/paper/accounts/${id}/signals`;
    if (strategyId) url += `?strategy_id=${strategyId}`;
    return this.request(url, { method: 'POST' });
  },
  executePaperSignals(id, strategyId) {
    var url = `/api/paper/accounts/${id}/execute`;
    if (strategyId) url += `?strategy_id=${strategyId}`;
    return this.request(url, { method: 'POST' });
  },
  getPaperLogs(id, limit = 100) {
    return this.request(`/api/paper/accounts/${id}/logs?limit=${limit}`);
  },
};

// 工具函数
function fmt(n, d = 2) {
  return Number(n).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
}
function signCls(n) { return n > 0 ? 'up' : (n < 0 ? 'down' : 'flat'); }
