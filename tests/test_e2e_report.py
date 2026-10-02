"""How the end-to-end script reports a check, without a browser."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import e2e  # noqa: E402


class NoBrowser:
    """Stands in for a browser context that no page was ever opened in."""

    def add_init_script(self, script):
        pass


def session() -> e2e.Session:
    return e2e.Session(NoBrowser(), "http://127.0.0.1:0", date(2026, 10, 31), None)


def test_a_check_that_fails_before_its_page_exists_is_reported_as_itself():
    def body(s):
        s.expect(False, "the seed has nothing to judge")

    assert e2e.problem_of(session(), body) == "Failed: the seed has nothing to judge"


def test_a_check_that_opens_no_page_and_passes_has_no_problem():
    assert e2e.problem_of(session(), lambda s: None) is None


def test_what_the_page_logged_is_reported_beside_the_failure():
    def body(s):
        s.errors.append("boom")
        s.noise.append("http 500: GET /api/settle")
        s.eq(1, 2, "the count")

    assert e2e.problem_of(session(), body) == \
        "Failed: the count: got 1, want 2 | also: page error: boom; http 500: GET /api/settle"
