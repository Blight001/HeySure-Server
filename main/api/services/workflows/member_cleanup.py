"""Foreign-key-safe workflow cleanup for deleted AI members."""

from sqlmodel import Session, select

from api.models import WorkflowConfirmation, WorkflowRecording


def detach_member_workflow_state(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
) -> None:
    """Preserve workflow history while removing references to one AI member."""
    confirmations = session.exec(select(WorkflowConfirmation).where(
        WorkflowConfirmation.requested_user_id == int(user_id),
        WorkflowConfirmation.ai_config_id == int(ai_config_id),
    )).all()
    recordings = session.exec(select(WorkflowRecording).where(
        WorkflowRecording.user_id == int(user_id),
        WorkflowRecording.ai_config_id == int(ai_config_id),
    )).all()
    for row in (*confirmations, *recordings):
        row.ai_config_id = None
        session.add(row)
    session.flush()
