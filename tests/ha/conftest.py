"""Fixtures for the Home Assistant layer tests.

These run against Home Assistant, on Linux, in CI - not on a developer's
Windows box, where the harness blocks sockets and the ProactorEventLoop needs a
local socket pair for its own self-pipe. That is expected; HA supports Linux,
macOS and the devcontainer for development.

They have not been executed yet, so expect some to need fixing the first time
they do. They skip when the harness is absent, so the pure-module suite one
level up still runs on a bare checkout and the default run does not imply
coverage that does not exist.

THIS CONFTEST LIVES IN ITS OWN DIRECTORY ON PURPOSE.

It declares an autouse fixture that pulls in Home Assistant machinery. A pytest
conftest applies to everything at or below its directory, so with this file in
`tests/` the fixture attached itself to the pure-module tests too and errored
every one of them at setup. Keeping it beside only the tests that need it is
what stops that.

The pure modules (analysis, fingerprint, attribution, verify) are tested one
level up, without Home Assistant at all, and load their targets by path.
"""

import pytest

# Skips this whole directory when Home Assistant is not installed, so the
# pure-module suite one level up still runs on a bare checkout.
pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.power_fingerprint.const import (
    CONF_CIRCUITS,
    CONF_MAINS,
    CONF_PAIRS,
    CONF_PRICE,
    CONF_TOLERANCE,
    DOMAIN,
)

MAINS = "sensor.mains_power"
CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Required for Home Assistant to load a custom component in tests."""
    return


@pytest.fixture
def config_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        title="Power Fingerprint",
        unique_id=DOMAIN,
        data={
            CONF_MAINS: MAINS,
            CONF_CIRCUITS: [CIRCUIT_A, CIRCUIT_B],
            CONF_PRICE: 0.13,
            CONF_TOLERANCE: 5.0,
            CONF_PAIRS: "",
        },
    )


@pytest.fixture
def powered(hass):
    """Put a plausible panel on the state machine.

    Circuits sum to 150 W against a 155 W mains reading - a 5 W remainder, well
    inside tolerance, so the coverage sensor should read healthy.
    """

    def _set(mains=155.0, a=100.0, b=50.0):
        for entity, value in ((MAINS, mains), (CIRCUIT_A, a), (CIRCUIT_B, b)):
            hass.states.async_set(
                entity,
                value,
                {
                    "device_class": "power",
                    "state_class": "measurement",
                    "unit_of_measurement": "W",
                },
            )

    _set()
    return _set
