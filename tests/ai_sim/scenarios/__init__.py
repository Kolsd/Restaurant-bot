# scenarios/__init__.py
# This package is built by a parallel agent.
# When available, ALL_SCENARIOS will be imported from here.
# Harness imports: from tests.ai_sim.scenarios import ALL_SCENARIOS

try:
    from tests.ai_sim.scenarios.mesa import MESA_SCENARIOS
    from tests.ai_sim.scenarios.delivery import DELIVERY_SCENARIOS
    from tests.ai_sim.scenarios.pickup import PICKUP_SCENARIOS
    from tests.ai_sim.scenarios.adversarial import ADVERSARIAL_SCENARIOS

    ALL_SCENARIOS = (
        MESA_SCENARIOS
        + DELIVERY_SCENARIOS
        + PICKUP_SCENARIOS
        + ADVERSARIAL_SCENARIOS
    )
except ImportError:
    # Graceful degradation while scenario files are being built
    ALL_SCENARIOS = []
