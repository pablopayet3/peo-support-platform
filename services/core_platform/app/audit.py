from sqlalchemy.orm import Session

from .models import AuditEvent


def record(db: Session, *, actor: str, action: str, entity: str, entity_id: str,
           request_id: str | None = None, **details) -> None:
    """Add one audit event to the current transaction. Never updated, never deleted."""
    db.add(AuditEvent(actor=actor, action=action, entity=entity, entity_id=entity_id,
                      request_id=request_id, details=details))
