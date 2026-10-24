import pytest

from hand.cli import demo_task


@pytest.fixture
def task_data():
    data = demo_task("test-task")
    data["approval_points"] = []
    return data
