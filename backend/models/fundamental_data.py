"""基本面 / 财务 / 新闻 / 宏观 / 增减持 数据模型

口径统一（与 standards.py 一致）：
- symbol 统一为 ``600519.SH`` 形式
- 金额统一为「元」，数量统一为「股」，比率统一为「%」
- 日期统一为 ``YYYY-MM-DD``（macro_data 的源日期格式不统一，另存原始文本 + 排序键）
"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Date, Text, Index
from sqlalchemy.sql import func
from database import Base


class Fundamental(Base):
    """公司基本面概览（估值/股本/市值快照，带 as_of_date 时间戳）"""
    __tablename__ = "fundamental"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False, comment="标准代码，如 600519.SH")
    name = Column(String(100), comment="公司名称")
    industry = Column(String(50), comment="行业")
    list_date = Column(Date, comment="上市日期")
    total_shares = Column(Float, comment="总股本（股）")
    float_shares = Column(Float, comment="流通股（股）")
    total_market_cap = Column(Float, comment="总市值（元）")
    float_market_cap = Column(Float, comment="流通市值（元）")
    pe_ttm = Column(Float, comment="市盈率 TTM")
    pb = Column(Float, comment="市净率 MRQ")
    turnover = Column(Float, comment="换手率（%）")
    as_of_date = Column(Date, comment="快照日期")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_fundamental_symbol", "symbol"),
    )


class FinancialStatement(Base):
    """财务三表（长表：每行一条报表项目，value 已标准化为统一单位）"""
    __tablename__ = "financial_statement"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False, comment="标准代码，如 600519.SH")
    statement_type = Column(String(20), nullable=False,
                            comment="报表类型：balance_sheet/income_statement/cash_flow")
    report_date = Column(Date, nullable=False, comment="报告期")
    item = Column(String(128), nullable=False, comment="报表项目名称")
    value = Column(Float, comment="项目数值")
    updated_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_fin_stmt_symbol_type_date", "symbol", "statement_type", "report_date"),
    )


class NewsArticle(Base):
    """个股/全球新闻（scope 区分；symbol 为空表示非个股口径）"""
    __tablename__ = "news_article"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), comment="标准代码（全球新闻为空）")
    scope = Column(String(20), nullable=False, default="ticker",
                   comment="ticker=个股新闻 / global=全球宏观新闻")
    title = Column(String(256), comment="标题")
    content = Column(Text, comment="正文摘要")
    source = Column(String(64), comment="来源")
    url = Column(String(512), comment="链接")
    publish_time = Column(String(19), comment="发布时间 YYYY-MM-DD HH:MM:SS")
    updated_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_news_symbol_time", "symbol", "publish_time"),
        Index("idx_news_scope_time", "scope", "publish_time"),
    )


class MacroData(Base):
    """宏观指标（国内：CPI/PPI/PMI/GDP/M2/LPR 等）"""
    __tablename__ = "macro_data"

    id = Column(Integer, primary_key=True, index=True)
    indicator = Column(String(40), nullable=False, comment="指标键：cpi_yearly/ppi/pmi/gdp/money_supply/lpr 等")
    date_text = Column(String(40), nullable=False, comment="源日期原文，如 2026年09月份")
    sort_date = Column(String(8), comment="排序键 YYYYMM（用于按时间排序/过滤）")
    value = Column(Float, comment="主值（今值/当月值）")
    prev = Column(Float, comment="前值")
    forecast = Column(Float, comment="预测值")
    updated_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_macro_indicator_sort", "indicator", "sort_date"),
    )


class InsiderTransaction(Base):
    """股东增减持（来自东财股东持仓变动）"""
    __tablename__ = "insider_transaction"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False, comment="标准代码，如 600519.SH")
    holder_name = Column(String(128), comment="股东名称")
    change_type = Column(String(20), comment="增减持方向：增持/减持")
    change_volume = Column(Float, comment="变动数量（股）")
    change_ratio = Column(Float, comment="占总股本比例（%）")
    start_date = Column(Date, comment="变动开始日")
    end_date = Column(Date, comment="变动截止日")
    announce_date = Column(Date, comment="公告日")
    updated_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_insider_symbol", "symbol"),
    )