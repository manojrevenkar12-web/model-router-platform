from decimal import Decimal

from mrp import router
from mrp.catalog import load
from mrp.eval.routing import calibrate
from mrp.health import Health


def test_classification_rules_and_hints():
    msg = lambda t: [{"role": "user", "content": t}]  # noqa: E731
    assert router.classify(msg("Extract the fields as JSON")) == "extraction"
    assert router.classify(msg("Classify the sentiment of this review")) == "classification"
    assert router.classify(msg("Fix the function below\n```python\ndef f(): pass\n```")) == "code"
    assert router.classify(msg("Summarize this thread")) == "summarization"
    assert router.classify(msg("Prove that the sum is even, step by step")) == "reasoning"
    assert router.classify(msg("hi!")) == "chat"
    assert router.classify(msg("hi!"), hint="code") == "code"
    assert router.classify(msg("anything"), response_format={"type": "json_object"}) == "extraction"


def test_checks():
    assert router.check("extraction", '{"a": 1}') and not router.check("extraction", '{"a": ')
    assert router.check("classification", "Positive") and not router.check("classification", "maybe?")
    assert router.check("code", "def f(x):\n    return x") and not router.check("code", "def f(x)\n")
    assert router.check("chat", "anything") is None


def test_lower_bound_prefers_proven_models():
    t = router.QualityTable()
    for _ in range(20):
        t.record("chat", "open-8b", True)  # 20/20 looks perfect but proves little
    for i in range(2000):
        t.record("chat", "general-b-mini", i % 50 != 0)  # 98% over 2,000
    cat = load()
    allowed = {"open-8b", "general-b-mini"}
    lb = router.decide(cat, t, "chat", 0.90, 100, 100, Decimal(1), allowed)
    pe = router.decide(cat, t, "chat", 0.90, 100, 100, Decimal(1), allowed, use_lower_bound=False)
    assert lb.primary.id == "general-b-mini" and pe.primary.id == "open-8b"


def test_cascade_only_when_checked_answers_still_meet_the_target():
    assert router.passed_quality(0.9, catch=0.75, alarm=0.01) > 0.97
    assert router.passed_quality(0.6, catch=0.2, alarm=0.01) < 0.7
    cat, table = load(), calibrate(300)
    r = router.decide(cat, table, "extraction", 0.96, 800, 60, Decimal(1))
    assert (
        r.first_try is None
        or router.passed_quality(
            table.cell("extraction", r.first_try.id).lower, *table.check_rates("extraction")
        )
        >= 0.96
    )
    assert r.estimated_cost <= r.primary.cost(800, 60)
    chat_route = router.decide(cat, table, "chat", 0.90, 100, 100, Decimal(1))
    assert chat_route.first_try is None  # no cheap check exists for chat


def test_budget_and_context_limits():
    cat, table = load(), calibrate(50)
    try:
        router.decide(cat, table, "reasoning", 0.9, 10_000, 1_000, Decimal("0.0000001"))
        raise AssertionError("expected NoRoute")
    except router.NoRoute:
        pass
    r = router.decide(cat, table, "chat", 0.9, 150_000, 1000, Decimal(10))
    assert r.primary.context >= 151_000


def test_health_takes_a_degraded_model_out():
    table = calibrate(300)
    h = Health(table)
    for i in range(200):
        out = h.observe("extraction", "open-8b-extract", i % 3 != 0)  # passes 67%, expected ~98%
        if out:
            break
    assert ("extraction", "open-8b-extract") in h.degraded and "open-8b-extract" in h.excluded("extraction")
    h2 = Health(table)
    for _ in range(200):
        h2.observe("extraction", "open-8b-extract", True)
    assert not h2.degraded
