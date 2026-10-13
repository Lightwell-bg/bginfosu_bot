import pytest

from bginfo.ai.ledger import AiLedger
from bginfo.ai.openrouter import AiError, AiUsage
from bginfo.db.conn import open_db


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "t.db")


def usage(cost):
    return AiUsage("m", 10, 5, cost, 100)


def test_budget(conn):
    led = AiLedger(conn, 1.0, {"jev": 30})
    led.check(1, "jev")
    led.record(1, "jev", "m", usage(0.6), True)
    led.check(1, "jev")
    led.record(2, "jev", "m", usage(0.5), True)
    with pytest.raises(AiError) as e:
        led.check(3, "jev")
    assert e.value.kind == "budget"


def test_user_limit(conn):
    led = AiLedger(conn, 100.0, {"explain": 2, "jev": 5})
    for _ in range(2):
        led.record(7, "explain", "m", usage(None), True)
    with pytest.raises(AiError) as e:
        led.check(7, "explain")
    assert e.value.kind == "limit"
    led.check(8, "explain")
    led.check(7, "jev")
    led.check(None, "explain")


def test_day_rollover(conn):
    led = AiLedger(conn, 1.0, {"jev": 1})
    conn.execute(
        "INSERT INTO ai_calls(at, tg_user_id, kind, model, ok, cost_usd) VALUES"
        " ('2000-01-01T23:59:59Z', 1, 'jev', 'm', 1, 5.0)"
    )
    led.check(1, "jev")  # вчерашние траты не считаются
    led.record(1, "jev", "m", usage(0.1), False, "timeout")
    with pytest.raises(AiError):
        led.check(1, "jev")
    rows = led.stats(30)
    assert len(rows) == 1 and rows[0]["calls"] == 1 and rows[0]["errors"] == 1
    assert rows[0]["input_tokens"] == 10


def test_null_cost_counts_as_conservative_estimate(conn):
    led = AiLedger(conn, 0.05, {}, unknown_cost_usd=0.02)
    for _ in range(2):
        led.check(1, "jev")
        led.record(1, "jev", "m", usage(None), True)
    led.check(1, "jev")  # 0.04 < 0.05
    led.record(1, "jev", "m", usage(None), True)
    with pytest.raises(AiError) as e:  # 0.06 >= 0.05
        led.check(1, "jev")
    assert e.value.kind == "budget"


def test_reserve_blocks_parallel_overspend(conn):
    led = AiLedger(conn, 0.025, {}, unknown_cost_usd=0.01)
    ids = [led.reserve(i, "jev", "m") for i in range(3)]  # 0.03 зарезервировано без ответов
    with pytest.raises(AiError) as e:
        led.reserve(9, "jev", "m")
    assert e.value.kind == "budget"
    led.settle(ids[0], usage(0.0), True)  # фактическая стоимость освобождает резерв
    led.reserve(9, "jev", "m")
    row = conn.execute("SELECT status, ok, cost_usd FROM ai_calls WHERE id=?", (ids[0],)).fetchone()
    assert (row["status"], row["ok"], row["cost_usd"]) == ("done", 1, 0.0)


def test_error_is_stored_as_fixed_code(conn):
    led = AiLedger(conn, 10.0, {})
    led.check(1, "explain")
    led.record(1, "explain", "m", usage(0.1), False, "в ответе появилось имя Иван Петров")
    led.check(1, "jev")
    led.record(1, "jev", "m", None, False, "timeout")
    errs = sorted(r[0] for r in conn.execute("SELECT error FROM ai_calls"))
    assert errs == ["rejected", "timeout"]


def test_limit_survives_user_deletion(conn):
    from bginfo.bot import repo

    led = AiLedger(conn, 100.0, {"jev": 2})
    repo.touch_user(conn, 5)
    for _ in range(2):
        led.check(5, "jev")
        led.record(5, "jev", "m", usage(0.01), True)
    repo.delete_user(conn, 5)  # ai_calls обезличены, но счётчик остаётся
    assert conn.execute("SELECT COUNT(*) FROM ai_calls WHERE tg_user_id IS NOT NULL").fetchone()[0] == 0
    repo.touch_user(conn, 5)
    with pytest.raises(AiError) as e:
        led.check(5, "jev")
    assert e.value.kind == "limit"
    hashes = [r[0] for r in conn.execute("SELECT user_hash FROM rate_counters")]
    assert hashes and all("5" != h and len(h) == 64 for h in hashes)


def test_cleanup_drops_old_rate_counters(conn):
    from bginfo.bot import repo

    conn.execute("INSERT INTO rate_counters VALUES ('h', '2000-01-01', 'jev', 3)")
    conn.execute("INSERT INTO rate_counters VALUES ('h', date('now'), 'jev', 1)")
    repo.cleanup(conn, 90)
    assert [r[0] for r in conn.execute("SELECT day FROM rate_counters")] != ["2000-01-01"]
    assert conn.execute("SELECT COUNT(*) FROM rate_counters").fetchone()[0] == 1


async def test_async_check_record_run_off_loop_and_pair(conn):
    import threading

    led = AiLedger(conn, 10.0, {"jev": 1})
    threads = []
    real = led.reserve

    def spy(*a, **k):
        threads.append(threading.current_thread())
        return real(*a, **k)

    led.reserve = spy
    await led.acheck(1, "jev")
    await led.arecord(1, "jev", "m", usage(0.2), True)
    assert threads and threads[0] is not threading.main_thread()
    row = conn.execute("SELECT status, ok, cost_usd FROM ai_calls").fetchone()
    assert (row["status"], row["ok"], row["cost_usd"]) == ("done", 1, 0.2)  # резерв закрыт, не вставлена вторая строка
    assert conn.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 1
    with pytest.raises(AiError) as e:
        await led.acheck(1, "jev")
    assert e.value.kind == "limit"
    led._exec.shutdown(wait=True)
