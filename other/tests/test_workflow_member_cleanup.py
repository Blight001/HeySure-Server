from api.models import WorkflowConfirmation, WorkflowRecording
from api.services.workflows.member_cleanup import detach_member_workflow_state


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows_by_model):
        self.rows_by_model = rows_by_model
        self.added = []
        self.flush_count = 0

    def exec(self, statement):
        model = statement.column_descriptions[0]["entity"]
        return _Rows(self.rows_by_model.get(model, []))

    def add(self, row):
        self.added.append(row)

    def flush(self):
        self.flush_count += 1


def test_detach_member_workflow_state_preserves_rows_and_clears_ai_reference():
    confirmation = WorkflowConfirmation(
        id="confirmation",
        run_id="run",
        step_id="step",
        requested_user_id=1,
        ai_config_id=19,
        expires_at=100.0,
    )
    recording = WorkflowRecording(
        id="recording",
        user_id=1,
        ai_config_id=19,
    )
    session = _Session({
        WorkflowConfirmation: [confirmation],
        WorkflowRecording: [recording],
    })

    detach_member_workflow_state(session, user_id=1, ai_config_id=19)

    assert confirmation.ai_config_id is None
    assert recording.ai_config_id is None
    assert session.added == [confirmation, recording]
    assert session.flush_count == 1
