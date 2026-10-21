# 蚂蚁量化系统 (Quant System)

一个功能完整的量化交易平台，提供数据管理、策略回测、模拟交易等功能。

## 项目结构

```
quant-system/
├── backend/          # 后端服务
│   ├── datasources/  # 数据源（新浪、东方财富、腾讯等）
│   ├── models/       # 数据模型
│   ├── routers/      # API 路由
│   ├── scripts/      # 脚本工具
│   ├── services/     # 业务逻辑
│   └── app.py        # FastAPI 应用入口
├── frontend/         # 前端页面
│   ├── js/           # JavaScript
│   └── *.html        # 页面
├── data/             # 数据目录
├── outputs/          # 输出文件
└── start.sh          # 启动脚本
```

## 功能特性

- 多数据源支持（新浪财经、东方财富、腾讯财经等）
- 策略回测系统
- 模拟交易
- 选股扫描（三周期均线策略等）
- 持仓管理
- 数据可视化

## 技术栈

**后端：**
- Python 3.10+
- FastAPI
- SQLite
- AKShare / 新浪 API / 东方财富 API

**前端：**
- HTML5 + CSS3 + JavaScript
- 响应式设计

## 快速开始

### 1. 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件配置 API keys
```

### 3. 启动服务

```bash
# 启动后端
cd backend
uvicorn app:app --reload --host 0.0.0.0 --port 8000

# 或使用启动脚本
./start.sh
```

### 4. 访问前端

打开浏览器访问：`http://localhost:8000`

## 数据源配置

项目支持多个数据源，可在 `backend/datasources/` 中配置：

- **新浪财经** - 实时行情
- **东方财富** - 历史数据
- **腾讯财经** - 补充数据
- **AKShare** - 开源数据接口

## 策略说明

### 三周期均线策略

扫描同时满足以下条件的股票：
- 5日、10日、20日均线多头排列
- 成交量放大
- 价格突破关键阻力位

## 开发计划

- [ ] 添加更多选股策略
- [ ] 完善回测系统
- [ ] 支持实盘交易
- [ ] 添加风险管理模块
- [ ] 性能优化

## 许可证

MIT License

## 免责声明

本项目仅供学习和研究使用，不构成投资建议。投资有风险，入市需谨慎。
