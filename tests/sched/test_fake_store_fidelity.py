"""The scheduler fake must share its rules with the real store (pass 7)."""
from nai.store import store as real
from tests.sched.fake_store import ACTIONS, EDGES, JOB_KINDS


def test_transition_table_is_identical():
    assert EDGES == real._ALLOWED_TRANSITIONS


def test_action_and_job_kind_sets_are_identical():
    assert ACTIONS == real.COMMAND_ACTIONS
    assert JOB_KINDS == real.JOB_KINDS
