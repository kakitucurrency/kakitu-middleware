import pytest
from stats import Stats


def make_worker(address='kshs_1abc', work_completed=0, kshs_earned=0.0):
    class FakeWorker:
        kshs_address = address
        work_completed = 0
        kshs_earned = 0.0
    w = FakeWorker()
    w.work_completed = work_completed
    w.kshs_earned = kshs_earned
    return w


def test_initial_state():
    s = Stats()
    d = s.to_dict()
    assert d['connected_workers'] == 0
    assert d['total_work_completed'] == 0
    assert d['total_kshs_paid'] == '0.000000'
    assert d['top_earners'] == []


def test_record_completion_updates_totals():
    s = Stats()
    w = make_worker('kshs_1abc')
    s.record_completion(w, 0.01)
    d = s.to_dict()
    assert d['total_work_completed'] == 1
    assert d['total_kshs_paid'] == '0.010000'


def test_top_earners_sorted_by_earned():
    s = Stats()
    w1 = make_worker('kshs_1aaa')
    w2 = make_worker('kshs_1bbb')
    s.record_completion(w1, 0.05)
    s.record_completion(w1, 0.05)
    s.record_completion(w2, 0.01)
    earners = s.to_dict()['top_earners']
    assert earners[0]['address'] == 'kshs_1aaa'
    assert earners[0]['earned'] == '0.100000'
    assert earners[1]['address'] == 'kshs_1bbb'


def test_top_earners_capped_at_ten():
    s = Stats()
    for i in range(15):
        w = make_worker(f'kshs_1addr{i:03d}')
        s.record_completion(w, float(i))
    assert len(s.to_dict()['top_earners']) == 10


def test_same_address_accumulates():
    s = Stats()
    w = make_worker('kshs_1abc')
    s.record_completion(w, 0.01)
    s.record_completion(w, 0.01)
    earners = s.to_dict()['top_earners']
    assert earners[0]['earned'] == '0.020000'
    assert earners[0]['work_completed'] == 2


def test_float_accumulation_precision():
    s = Stats()
    for _ in range(100):
        w = make_worker('kshs_1addr')
        s.record_completion(w, 0.01)
    assert s.to_dict()['total_kshs_paid'] == '1.000000'
