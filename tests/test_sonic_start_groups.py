import pytest

from netopsbench.platform.topology.sonic_start_group import GROUPS, start_group


class Proxy:
    def __init__(self, names, fail=None):
        self.states = {n: "STOPPED" for n in names}
        self.calls = []
        self.fail = fail
        self.peak = 0

    def getAllProcessInfo(self):
        return [dict(name=n, statename=s, pid=1) for n, s in self.states.items()]

    def startProcess(self, n, wait):
        assert wait is False
        self.calls.append(n)
        self.states[n] = "STARTING"
        self.peak = max(self.peak, sum(s == "STARTING" for s in self.states.values()))

    def advance(self, seconds):
        self.states = {n: ("EXITED" if n == self.fail else "RUNNING") for n in self.states}


def test_bound_and_events():
    p = Proxy(["a", "b", "c", "d"])
    events = []
    start_group(p, list(p.states), 10, lambda **x: events.append(x), clock=lambda: 0, sleep=p.advance)
    assert p.peak == 4 and len([x for x in events if x["event"] == "running"]) == 4
    assert [x["event"] for x in events][-1] == "group_end"


def test_failure_no_restart():
    p = Proxy(["a", "b"], "b")
    with pytest.raises(RuntimeError):
        start_group(p, ["a", "b"], 10, lambda **x: None, clock=lambda: 0, sleep=p.advance)
    assert p.calls == ["a", "b"]


def test_deadline():
    p = Proxy(["a"])
    with pytest.raises(TimeoutError):
        start_group(p, ["a"], 0, lambda **x: None, clock=lambda: 0)
    assert not p.calls


def test_cancel():
    p = Proxy(["a"])
    with pytest.raises(InterruptedError):
        start_group(p, ["a"], 10, lambda **x: None, clock=lambda: 0, cancelled=lambda: True)
    assert not p.calls


def test_dependency_groups():
    position = {n: i for i, g in enumerate(GROUPS) for n in g}
    assert max(map(len, GROUPS)) == 4
    for a, b in [("teamsyncd", "teammgrd"), ("fpmsyncd", "zebra"), ("zebra", "staticd"), ("natmgrd", "natsyncd")]:
        assert position[a] < position[b]


@pytest.mark.parametrize("names", [["a"] * 2, ["a", "b", "c", "d", "e"]])
def test_invalid_group_rejected_before_rpc(names):
    p = Proxy(names)
    with pytest.raises(ValueError):
        start_group(p, names, 10, lambda **x: None, clock=lambda: 0)
    assert not p.calls


def test_missing_service():
    p = Proxy([])
    with pytest.raises(RuntimeError, match="Missing service"):
        start_group(p, ["a"], 10, lambda **x: None, clock=lambda: 0)
    assert not p.calls


def test_timeout_after_submission_does_not_restart():
    p = Proxy(["a"])
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(TimeoutError):
        start_group(p, ["a"], 1, lambda **x: None, clock=lambda: now[0], sleep=sleep)
    assert p.calls == ["a"]


def test_rpc_exception_propagated():
    p = Proxy(["a"])
    error = OSError("injected")

    def fail(*args):
        raise error

    p.startProcess = fail
    with pytest.raises(OSError) as got:
        start_group(p, ["a"], 10, lambda **x: None, clock=lambda: 0)
    assert got.value is error
