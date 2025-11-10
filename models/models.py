from sqlalchemy import Column, Integer, String, Date, Numeric,Float
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class Instrument(Base):
    __tablename__ = 'instruments'
    __table_args__ = {'schema': 'public'}

    token = Column(String(50), primary_key=True)
    symbol = Column(String(50), nullable=False)
    name = Column(String(100))
    expiry = Column(Date)
    strike = Column(Numeric(20, 6))   # 🔼 Increased precision
    lotsize = Column(Integer)
    instrumenttype = Column(String(20))
    exch_seg = Column(String(20))
    tick_size = Column(Numeric(20, 6))  # 🔼 Increased precision


class HistoricStock(Base):
    __tablename__ = "historicstocks"
    
    date = Column(Date, nullable=True)
    open = Column(Float, nullable=True)
    close = Column(Float, nullable=True)
    high = Column(Float, nullable=True)
    low = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    symboltoken = Column(String(2000), primary_key=True)
    exchange = Column(String(1000), nullable=True)
    symbol = Column(String(200), nullable=True)    