# main.py
from datetime import datetime
from typing import List, Optional
from fastapi import FastAPI, Depends, Query, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from tel.models import get_session, Signal, create_tables

app = FastAPI(
    title="Telegram Signal Monitor & Stats API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.on_event("startup")
def on_startup():
    """Ensure database tables exist when the API starts."""
    create_tables()


# --- Pydantic Response Schemas ---

class SignalOut(BaseModel):
    id: int
    telegram_message_id: Optional[int]
    action: str
    pair: str
    entry: Optional[str]
    tp: Optional[str]
    sl: Optional[float]
    raw_snippet: str
    created_at: datetime

    class Config:
        from_attributes = True


class PairFrequency(BaseModel):
    pair: str
    count: int


class ActionDistribution(BaseModel):
    longs: int
    shorts: int
    other: int


class SignalMetrics(BaseModel):
    total_signals: int
    actions: ActionDistribution
    unique_pairs_count: int
    top_pairs: List[PairFrequency]
    last_signal_at: Optional[datetime]


# --- Endpoints ---

@app.get("/health", tags=["System"])
def health_check():
    """Health check endpoint to verify API uptime."""
    return {"status": "online", "server_time": datetime.utcnow().isoformat()}


@app.get("/stats", response_model=SignalMetrics, tags=["Analytics"])
def get_overall_stats(db: Session = Depends(get_session)):
    """
    Get aggregated system statistics:
    - Total signals logged
    - Long vs Short breakdown
    - Unique traded pairs
    - Top 5 most active pairs
    - Timestamp of the latest signal
    """
    # 1. Total Signals
    total_count = db.query(func.count(Signal.id)).scalar() or 0

    # 2. Action Breakdown
    action_counts = dict(
        db.query(Signal.action, func.count(Signal.id))
        .group_by(Signal.action)
        .all()
    )
    longs = action_counts.get("LONG", 0)
    shorts = action_counts.get("SHORT", 0)
    other = sum(v for k, v in action_counts.items() if k not in ("LONG", "SHORT"))

    # 3. Unique Pairs Count
    unique_pairs = db.query(func.count(func.distinct(Signal.pair))).scalar() or 0

    # 4. Top 5 Most Active Pairs
    top_pairs_rows = (
        db.query(Signal.pair, func.count(Signal.id).label("freq"))
        .group_by(Signal.pair)
        .order_by(desc("freq"))
        .limit(5)
        .all()
    )
    top_pairs = [PairFrequency(pair=row[0], count=row[1]) for row in top_pairs_rows]

    # 5. Timestamp of Most Recent Signal
    latest_record = db.query(Signal.created_at).order_by(desc(Signal.created_at)).first()
    last_signal_at = latest_record[0] if latest_record else None

    return SignalMetrics(
        total_signals=total_count,
        actions=ActionDistribution(longs=longs, shorts=shorts, other=other),
        unique_pairs_count=unique_pairs,
        top_pairs=top_pairs,
        last_signal_at=last_signal_at,
    )


@app.get("/stats/pairs", response_model=List[PairFrequency], tags=["Analytics"])
def get_pair_breakdown(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_session)
):
    """Returns frequency counts grouped by trading pair."""
    results = (
        db.query(Signal.pair, func.count(Signal.id).label("count"))
        .group_by(Signal.pair)
        .order_by(desc("count"))
        .limit(limit)
        .all()
    )
    return [PairFrequency(pair=row[0], count=row[1]) for row in results]


@app.get("/signals", response_model=List[SignalOut], tags=["Signals"])
def list_signals(
    pair: Optional[str] = Query(None, description="Filter by pair, e.g. APTUSDT"),
    action: Optional[str] = Query(None, description="Filter by action: LONG or SHORT"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_session),
):
    """
    Paginated list of saved signals with optional filtering by symbol and direction.
    """
    query = db.query(Signal)

    if pair:
        clean_pair = pair.upper().replace("/", "").replace("-", "").strip()
        query = query.filter(Signal.pair == clean_pair)

    if action:
        query = query.filter(Signal.action == action.upper().strip())

    return query.order_by(desc(Signal.created_at)).offset(offset).limit(limit).all()


@app.get("/signals/{signal_id}", response_model=SignalOut, tags=["Signals"])
def get_signal_by_id(signal_id: int, db: Session = Depends(get_session)):
    """Fetch an individual signal record by database ID."""
    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Signal with ID {signal_id} not found."
        )
    return signal