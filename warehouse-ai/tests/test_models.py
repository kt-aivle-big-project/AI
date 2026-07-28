import pytest
from pydantic import ValidationError

from app.models import NaturalLanguageCommand, RobotEvent


def test_natural_language_command_requires_text() -> None:
    with pytest.raises(ValidationError):
        NaturalLanguageCommand(warehouse_id=1, text="")


def test_completed_event_requires_work_id() -> None:
    with pytest.raises(ValidationError):
        RobotEvent(
            warehouse_id=1,
            robot_id="R1",
            event_type="TASK_COMPLETED",
        )

