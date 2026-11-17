# SPDX-License-Identifier: MIT
"""Opt-in unloaded PSU test: --keysight-live --keysight-host HOST.

Exercises 1 V / 50 mA and 2 V / 100 mA on each independent output. All outputs
finish OFF, with their original voltage/current settings restored. Readings use
the PSU's own measurement system; this is not calibration or a loaded CC test.
Socket and settling deadlines replace the process-killing pytest watchdog so
cleanup can complete after a failure.
"""

import json
import logging
import time
from dataclasses import asdict

import pytest

from alx.errors import InstrumentError
from alx.psu.keysight_e36313a import KeysightE36313A, Limits

log = logging.getLogger(__name__)
CHANNELS = (1, 2, 3)


def _wait_voltage(psu, channel, expected):
    deadline = time.monotonic() + 5.0
    while True:
        measured = psu.measure_v(channel)
        if abs(measured - expected) <= 0.05:
            return measured
        if time.monotonic() >= deadline:
            pytest.fail(f"CH{channel}: measured {measured:g} V, expected {expected:g} V +/- 0.05 V")
        time.sleep(0.1)


def _restore(host, initial):
    final = {}
    errors = []
    off = set()
    for channel in CHANNELS:
        psu = None
        try:
            psu = KeysightE36313A(host)
            psu.output_off(channel)
            _wait_voltage(psu, channel, 0.0)
            off.add(channel)
        except (InstrumentError, pytest.fail.Exception) as exc:
            errors.append(f"CH{channel} output OFF could not be verified: {exc}")
        finally:
            if psu is not None:
                psu.close()
    for channel in sorted(off):
        psu = None
        try:
            saved = initial[channel]
            limits = {
                channel: Limits(
                    v_max=KeysightE36313A.V_MAX[channel - 1],
                    i_max=KeysightE36313A.I_MAX[channel - 1],
                )
            }
            psu = KeysightE36313A(host, limits=limits)
            psu.set_current(saved.setpoint_i, channel)
            psu.set_voltage(saved.setpoint_v, channel)
            state = psu.state(channel)
            if state.output_on:
                raise InstrumentError("Output unexpectedly ON after restoring settings")
            final[channel] = asdict(state)
        except InstrumentError as exc:
            errors.append(f"CH{channel} settings could not be restored: {exc}")
        finally:
            if psu is not None:
                psu.close()
    return final, errors


@pytest.fixture
def live_psu(request, record_property):
    if not request.config.getoption("--keysight-live"):
        pytest.skip("Requires --keysight-live and an unloaded PSU")
    host = request.config.getoption("--keysight-host")
    if not host:
        pytest.fail("--keysight-host is required with --keysight-live")
    limits = {channel: Limits(v_max=2.1, i_max=0.11) for channel in CHANNELS}
    psu = KeysightE36313A(host, limits=limits)
    try:
        initial = {channel: psu.state(channel) for channel in CHANNELS}
        record_property("instrument", psu.idn)
        record_property("initial", json.dumps({c: asdict(s) for c, s in initial.items()}))
        for channel, state in initial.items():
            assert 0 <= state.setpoint_v <= KeysightE36313A.V_MAX[channel - 1]
            assert 0 <= state.setpoint_i <= KeysightE36313A.I_MAX[channel - 1]
            assert psu._query(f"VOLT:MODE? (@{channel})") == "FIX"
            assert psu._query(f"CURR:MODE? (@{channel})") == "FIX"
        psu._independent()
    except BaseException:
        psu.close()
        raise
    try:
        for channel in CHANNELS:
            psu.output_off(channel)
            _wait_voltage(psu, channel, 0.0)
        yield psu
    finally:
        psu.close()
        final, errors = _restore(host, initial)
        record_property("final", json.dumps(final))
        log.info("Final PSU state: %s", json.dumps(final))
        if errors:
            pytest.fail("; ".join(errors))


@pytest.mark.timeout(0)
def test_ALX1753_P20_live_unloaded_channels(live_psu, record_property):
    psu = live_psu
    samples = []
    try:
        for channel in CHANNELS:
            for volts, amps in ((1.0, 0.05), (2.0, 0.1)):
                psu.output_off(channel)
                _wait_voltage(psu, channel, 0.0)
                assert psu.set_current(amps, channel) == pytest.approx(amps, abs=0.001)
                assert psu.set_voltage(volts, channel) == pytest.approx(volts, abs=0.002)
                psu.output_on(channel)
                measured_v = _wait_voltage(psu, channel, volts)
                measured_i = psu.measure_i(channel)
                sample = {
                    "channel": channel,
                    "setpoint_v": volts,
                    "setpoint_i": amps,
                    "measure_v": measured_v,
                    "measure_i": measured_i,
                }
                samples.append(sample)
                log.info("Unloaded PSU sample: %s", json.dumps(sample))
                assert abs(measured_i) <= 0.01
                for other in CHANNELS:
                    if other != channel:
                        assert not psu.output_is_on(other)
                        assert abs(psu.measure_v(other)) <= 0.05
                psu.output_off(channel)
                _wait_voltage(psu, channel, 0.0)
    finally:
        record_property("samples", json.dumps(samples))
