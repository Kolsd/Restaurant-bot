# scenarios/__init__.py
# This package is built by a parallel agent.
# When available, ALL_SCENARIOS will be imported from here.
# Harness imports: from tests.ai_sim.scenarios import ALL_SCENARIOS

try:
    from tests.ai_sim.scenarios.mesa import MESA_SCENARIOS
    from tests.ai_sim.scenarios.adversarial import ADVERSARIAL_SCENARIOS

    # The delivery/ and pickup/ suites drove the WhatsApp delivery funnel,
    # retired in chunk 9 of the web delivery wave (docs/claude/delivery-web.md).
    # Delivery and pickup now live on the web channel and are covered by
    # tests/test_delivery_*.py; running those scenarios would only burn LLM
    # credit failing against tools that no longer exist.
    ALL_SCENARIOS = (
        MESA_SCENARIOS
        + ADVERSARIAL_SCENARIOS
    )
except ImportError:
    # Graceful degradation while scenario files are being built
    ALL_SCENARIOS = []
