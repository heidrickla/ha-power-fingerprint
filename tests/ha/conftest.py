"""Fixtures for the Home Assistant layer tests.

These run against Home Assistant, on Linux, in CI - not on a developer's
Windows box, where the harness blocks sockets and the ProactorEventLoop needs a
local socket pair for its own self-pipe. That is expected; HA supports Linux,
macOS and the devcontainer for development.

They skip when the harness is absent, so the pure-module suite one level up
still runs on a bare checkout and the default run does not imply coverage that
does not exist.

THIS CONFTEST LIVES IN ITS OWN DIRECTORY ON PURPOSE.

It declares an autouse fixture that pulls in Home Assistant machinery. A pytest
conftest applies to everything at or below its directory, so with this file in
`tests/` the fixture attached itself to the pure-module tests too and errored
every one of them at setup. Keeping it beside only the tests that need it is
what stops that.

The pure modules (analysis, fingerprint, attribution, verify) are tested one
level up, without Home Assistant at all, and load their targets by path.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest

# Skips this whole directory when Home Assistant is not installed, so the
# pure-module suite one level up still runs on a bare checkout.
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import State
from homeassistant.util import dt as dt_util
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


class _Recorder:
    """Stands in for the recorder instance the actions ask for a job.

    Runs the query function inline rather than on a thread: the fake history
    is a dict lookup, and a real executor job would need a real recorder.
    """

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def history():
    """Answer every recorder history query from traces the test supplies.

    Both the coordinator's startup seed and the learn/map actions go through
    `history.get_significant_states`, so patching it there drives the real
    code path in both - including the unit conversion, the downsampling and
    the minimal-response filtering.

    Yields a dict of entity id to (timestamp, watts) rows. Values are stated
    in watts and read back through whatever unit the live state declares,
    which is how the integration itself reads them.
    """
    traces = {}

    def _significant_states(hass, start, end, entity_ids=None, **kwargs):
        out = {}
        for entity in entity_ids or []:
            rows = traces.get(entity)
            if not rows:
                continue
            out[entity] = [
                State(entity, str(value), {}, last_changed=stamp)
                for stamp, value in rows
                if start <= stamp <= end
            ]
        return out

    with (
        patch(
            "homeassistant.components.recorder.history.get_significant_states",
            _significant_states,
        ),
        patch("homeassistant.helpers.recorder.get_instance", return_value=_Recorder()),
    ):
        yield traces


@pytest.fixture
def cycling():
    """Build a trace of one appliance cycling, as the recorder would keep it.

    Long enough runs and gaps for `segment` to find discrete events and for
    `cadence` to learn a rhythm from when they happened.
    """

    def _build(watts, idle=2.0, runs=8, step_s=30, hours_apart=2):
        rows = []
        stamp = dt_util.utcnow() - timedelta(hours=runs * hours_apart)
        for _ in range(runs):
            for _ in range(4):
                rows.append((stamp, idle))
                stamp += timedelta(seconds=step_s)
            for _ in range(10):
                rows.append((stamp, watts))
                stamp += timedelta(seconds=step_s)
            for _ in range(4):
                rows.append((stamp, idle))
                stamp += timedelta(seconds=step_s)
            stamp += timedelta(hours=hours_apart) - timedelta(seconds=18 * step_s)
        return rows

    return _build
