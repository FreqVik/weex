# tasks.py
from tel.models import SessionLocal, Signal


def save_signal_to_db(signal_data: dict) -> int:
    """RQ background task to persist parsed signals to the database."""
    session = SessionLocal()
    try:
        # Check for duplicate telegram_message_id
        msg_id = signal_data.get("message_id")
        if msg_id:
            existing = session.query(Signal).filter_by(telegram_message_id=msg_id).first()
            if existing:
                return existing.id

        signal_record = Signal(
            telegram_message_id=msg_id,
            action=signal_data["action"],
            pair=signal_data["pair"],
            entry=str(signal_data.get("entry", "CMP")),
            tp=str(signal_data.get("tp", "")),
            sl=signal_data.get("sl"),
            raw_snippet=signal_data.get("raw_snippet", "")
        )
        session.add(signal_record)
        session.commit()
        session.refresh(signal_record)
        return signal_record.id
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()