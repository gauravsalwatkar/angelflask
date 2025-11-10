from datetime import datetime
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from models.models import Instrument ,HistoricStock
from db import get_db
import requests
from sqlalchemy import desc
import csv
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
import yfinance as yf
# from models.models import Instrument, HistoricStock
# from database import get_db
from datetime import datetime, timedelta
router = APIRouter()

def clean_date(value):
    """Convert expiry string to date or None."""
    if not value or value == "":
        return None
    try:
        return datetime.strptime(value, "%d-%b-%Y").date()  # Angel API format: '27-Feb-2025'
    except Exception:
        return None

def clean_decimal(value):
    """Convert empty or invalid numeric strings to None."""
    try:
        return float(value) if value not in ("", None) else None
    except Exception:
        return None

@router.post("/stocknames")
async def save_stock_names(db: Session = Depends(get_db)):
    # db = SessionLocal()
    try:
        response = requests.get(
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        )
        response.raise_for_status()
        stock_data = response.json()

        # Fetch existing tokens to avoid duplicates
        existing_tokens = {str(t[0]) for t in db.query(Instrument.token).all()}

        instruments_to_add = []

        for item in stock_data:
            token = str(item.get("token"))
            if token in existing_tokens:
                continue  # Skip duplicate token

            instrument = Instrument(
                token=token,
                symbol=item.get("symbol"),
                name=item.get("name"),
                expiry=clean_date(item.get("expiry")),
                strike=clean_decimal(item.get("strike")),
                lotsize=int(item.get("lotsize", 0) or 0),
                instrumenttype=item.get("instrumenttype"),
                exch_seg=item.get("exch_seg"),
                tick_size=clean_decimal(item.get("tick_size")),
            )
            instruments_to_add.append(instrument)

        # Insert in batches to avoid memory issues
        BATCH_SIZE = 500
        for i in range(0, len(instruments_to_add), BATCH_SIZE):
            db.bulk_save_objects(instruments_to_add[i:i+BATCH_SIZE])
            db.commit()

        return {"message": "Stock data saved successfully", "count": len(instruments_to_add)}

    except Exception as e:
        db.rollback()
        return {"error": str(e)}
    


@router.post("/fetch-historic-stocks")
def fetch_and_store_historic_stocks(db: Session = Depends(get_db), limit: int = 10):
    """
    Fetch historical stock data from Yahoo Finance for instruments in DB
    and store it in the historicstocks table.
    Logs instruments with missing data to CSV.
    """
    instruments = db.query(Instrument).order_by(desc(Instrument.name)).limit(limit).all()
    stored_count = 0
    failed_instruments = []

    # Calculate last 1 year
    end_date = datetime.today()
    start_date = end_date - timedelta(days=365)

    for inst in instruments:
        symbol = f"{inst.name}.BO"
        try:
            ticker = yf.Ticker(symbol)
            # Fetch last 1 year data
            hist = ticker.history(start=start_date, end=end_date)

            if hist.empty:
                failed_instruments.append({
                    "symbol": inst.symbol,
                    "token": inst.token,
                    "exchange": inst.exch_seg
                })
                continue  # skip storing if no data

            for date, row in hist.iterrows():
                stock_record = HistoricStock(
                        date=date.date(),
                        open=float(row["Open"]),
                        close=float(row["Close"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        volume=float(row["Volume"]),
                        symboltoken=str(inst.token),
                        exchange=inst.exch_seg,
                        symbol=inst.symbol
                    )
                db.add(stock_record)

            db.commit()
            stored_count += 1

        except Exception as e:
            db.rollback()
            print(f"Error fetching {symbol}: {e}")
            failed_instruments.append({
                "symbol": inst.symbol,
                "token": inst.token,
                "exchange": inst.exch_seg
            })

    # Write failed instruments to CSV
    csv_file = None
    if failed_instruments:
        csv_file = f"failed_instruments_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(csv_file, mode="w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=["symbol", "token", "exchange"])
            writer.writeheader()
            writer.writerows(failed_instruments)

    return {
        "message": f"Historic stock data stored for {stored_count} instruments",
        "failed_count": len(failed_instruments),
        "failed_csv": csv_file
    }