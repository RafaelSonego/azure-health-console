import logging

import pytest
from werkzeug._internal import _log

from healthconsole import cli

REQUEST = '127.0.0.1 - - [17/Nov/2026 10:00:00] "GET /api/triage HTTP/1.1" 200 -'


@pytest.fixture(autouse=True)
def keep_levels():
    loggers = [logging.getLogger(n) for n in ("", "werkzeug", "healthconsole.calls")]
    levels = [lg.level for lg in loggers]
    yield
    for lg, level in zip(loggers, levels):
        lg.setLevel(level)


@pytest.mark.parametrize("verbose, log_calls, shown", [(False, False, False), (True, False, True), (False, True, False),
                                                       (True, True, False)])
def test_request_lines_only_with_verbose(caplog, verbose, log_calls, shown):
    cli._setup_logging(verbose, log_calls)
    _log("info", REQUEST)  # how Flask's server logs each request
    assert (REQUEST in caplog.messages) is shown


def test_warnings_still_shown_by_default(caplog):
    cli._setup_logging(False, False)
    logging.getLogger("healthconsole.web").warning("something broke")
    assert "something broke" in caplog.messages
