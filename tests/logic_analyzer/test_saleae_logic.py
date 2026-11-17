# SPDX-License-Identifier: MIT
"""Saleae selection and capture configuration without connecting to Logic 2."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from saleae import automation

import alx.logic_analyzer.saleae_logic as saleae
from alx.errors import InstrumentError

grpc = importlib.import_module("grpc")


def device(device_id="device-a", model=automation.DeviceType.LOGIC_PRO_16, simulation=False):
    return automation.DeviceDesc(device_id=device_id, device_type=model, is_simulation=simulation)


class FakeManager:
    def __init__(self, devices):
        self.devices = devices
        self.discovery = []
        self.started = []
        self.close_count = 0
        self.capture = object()
        self.info = SimpleNamespace(version="2.4.0")
        self.failure = None
        self.close_failure = None
        self.connect_failure = None
        self.launch_failure = None
        self.launches = []

    def get_devices(self, *, include_simulation_devices):
        self.discovery.append(include_simulation_devices)
        if self.failure is not None:
            raise self.failure
        return self.devices

    def get_app_info(self):
        return self.info

    def start_capture(self, **kwargs):
        self.started.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.capture

    def close(self):
        self.close_count += 1
        if self.close_failure is not None:
            raise self.close_failure


@pytest.fixture
def bench(monkeypatch):
    def make(devices=None):
        manager = FakeManager([device()] if devices is None else devices)
        connected = []

        def connect(**kwargs):
            connected.append(kwargs)
            if manager.connect_failure is not None:
                raise manager.connect_failure
            return manager

        def launch(**kwargs):
            manager.launches.append(kwargs)
            if manager.launch_failure is not None:
                raise manager.launch_failure
            return manager

        api = SimpleNamespace(
            Manager=SimpleNamespace(connect=connect, launch=launch),
            DeviceType=automation.DeviceType,
            LogicDeviceConfiguration=automation.LogicDeviceConfiguration,
            CaptureConfiguration=automation.CaptureConfiguration,
            TimedCaptureMode=automation.TimedCaptureMode,
        )
        monkeypatch.setattr(saleae, "_automation", lambda: api)
        return manager, connected

    return make


def capture(logic, **changes):
    config = {
        "duration_s": 0.1,
        "digital_channels": [0, 15],
        "digital_sample_rate": 1_000_000,
        "digital_threshold_volts": 3.3,
    }
    return logic.start_capture(**(config | changes))


def test_ALX1753_P21_connect_discovers_real_device_without_starting_capture(bench):
    manager, connected = bench()
    logic = saleae.SaleaeLogic(host="logic.invalid", port=10431, timeout_s=2.0)
    assert connected == [
        {"address": "logic.invalid", "port": 10431, "connect_timeout_seconds": 2.0}
    ]
    assert manager.discovery == [False]
    assert logic.identity is manager.devices[0]
    assert logic.app_info is manager.info
    assert manager.started == []
    assert logic.launched is False
    assert manager.launches == []
    logic.close()
    logic.close()
    assert manager.close_count == 1


@pytest.mark.parametrize("devices", [[], [device(), device("device-b")], [device(simulation=True)]])
def test_ALX1753_P22_automatic_selection_requires_one_real_device(bench, devices):
    manager, _ = bench(devices)
    with pytest.raises(InstrumentError):
        saleae.SaleaeLogic()
    assert manager.close_count == 1
    assert manager.started == []


def test_ALX1753_P22_explicit_identity_selects_only_requested_device(bench):
    manager, _ = bench([device(), device("device-b"), device("simulated", simulation=True)])
    with saleae.SaleaeLogic("device-b") as logic:
        assert logic.identity is manager.devices[1]
    assert manager.close_count == 1


@pytest.mark.parametrize("requested", ["missing", "simulated"])
def test_ALX1753_P22_missing_or_simulated_identity_is_rejected(bench, requested):
    manager, _ = bench([device(), device("simulated", simulation=True)])
    with pytest.raises(InstrumentError):
        saleae.SaleaeLogic(requested)
    assert manager.close_count == 1


def test_ALX1753_P23_initialization_failure_closes_manager_and_preserves_error(bench):
    manager, _ = bench()
    failure = RuntimeError("native discovery failure")
    manager.failure = failure
    with pytest.raises(RuntimeError) as result:
        saleae.SaleaeLogic()
    assert result.value is failure
    assert manager.close_count == 1


@pytest.mark.parametrize(
    "model",
    [automation.DeviceType.LOGIC, automation.DeviceType.LOGIC_4, automation.DeviceType.LOGIC_16],
)
def test_ALX1753_P23_unsupported_hardware_is_rejected_and_disconnected(bench, model):
    manager, _ = bench([device(model=model)])
    with pytest.raises(InstrumentError):
        saleae.SaleaeLogic()
    assert manager.close_count == 1
    assert manager.started == []


def test_ALX1753_P24_capture_passes_native_configs_and_returns_native_capture(bench):
    manager, _ = bench()
    logic = saleae.SaleaeLogic()
    result = capture(logic, analog_channels=[1], analog_sample_rate=125_000)
    assert result is manager.capture
    call = manager.started[0]
    assert call["device_id"] == "device-a"
    config = call["device_configuration"]
    assert isinstance(config, automation.LogicDeviceConfiguration)
    assert config.enabled_digital_channels == [0, 15]
    assert config.digital_sample_rate == 1_000_000
    assert config.digital_threshold_volts == 3.3
    assert config.enabled_analog_channels == [1]
    assert config.analog_sample_rate == 125_000
    mode = call["capture_configuration"].capture_mode
    assert isinstance(mode, automation.TimedCaptureMode)
    assert mode.duration_seconds == 0.1
    assert manager.close_count == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_s": 0},
        {"duration_s": -1},
        {"duration_s": float("nan")},
        {"duration_s": float("inf")},
        {"duration_s": True},
        {"digital_channels": []},
        {"digital_channels": [0, 0]},
        {"digital_channels": [-1]},
        {"digital_channels": [16]},
        {"digital_channels": [True]},
        {"digital_channels": [1.0]},
        {"digital_sample_rate": 0},
        {"digital_sample_rate": True},
        {"digital_sample_rate": 1.5},
        {"digital_threshold_volts": None},
        {"digital_threshold_volts": 2.5},
        {"digital_threshold_volts": float("nan")},
        {"analog_channels": [1, 1], "analog_sample_rate": 125_000},
        {"analog_channels": [16], "analog_sample_rate": 125_000},
        {"analog_channels": [1]},
        {"analog_sample_rate": 125_000},
        {"analog_channels": [1], "analog_sample_rate": 0},
    ],
)
def test_ALX1753_P25_invalid_capture_config_is_refused_before_start(bench, changes):
    manager, _ = bench()
    logic = saleae.SaleaeLogic()
    with pytest.raises(InstrumentError):
        capture(logic, **changes)
    assert manager.started == []


@pytest.mark.parametrize(
    "model", [automation.DeviceType.LOGIC_8, automation.DeviceType.LOGIC_PRO_8]
)
def test_ALX1753_P26_eight_channel_models_reject_channel_eight(bench, model):
    manager, _ = bench([device(model=model)])
    logic = saleae.SaleaeLogic()
    threshold = None if model == automation.DeviceType.LOGIC_8 else 1.8
    with pytest.raises(InstrumentError):
        capture(logic, digital_channels=[8], digital_threshold_volts=threshold)
    assert manager.started == []


@pytest.mark.parametrize("threshold", [None, 0])
def test_ALX1753_P27_logic_eight_accepts_its_fixed_threshold(bench, threshold):
    manager, _ = bench([device(model=automation.DeviceType.LOGIC_8)])
    logic = saleae.SaleaeLogic()
    assert (
        capture(logic, digital_channels=[0, 7], digital_threshold_volts=threshold)
        is manager.capture
    )


def test_ALX1753_P27_logic_eight_rejects_pro_threshold(bench):
    manager, _ = bench([device(model=automation.DeviceType.LOGIC_8)])
    logic = saleae.SaleaeLogic()
    with pytest.raises(InstrumentError):
        capture(logic, digital_channels=[0])
    assert manager.started == []


def test_ALX1753_P28_native_capture_failure_is_not_wrapped(bench):
    manager, _ = bench()
    logic = saleae.SaleaeLogic()
    failure = RuntimeError("native capture failure")
    manager.failure = failure
    with pytest.raises(RuntimeError) as result:
        capture(logic)
    assert result.value is failure


def test_ALX1753_P29_closed_adapter_cannot_start_capture(bench):
    manager, _ = bench()
    with saleae.SaleaeLogic() as logic:
        pass
    with pytest.raises(InstrumentError):
        capture(logic)
    assert manager.started == []


def test_ALX1753_P30_missing_dependency_reports_installation_guidance(monkeypatch):
    def unavailable(name):
        raise ModuleNotFoundError("No module named saleae", name="saleae")

    monkeypatch.setattr(importlib, "import_module", unavailable)
    with pytest.raises(InstrumentError, match=r"(?i)install|extra|saleae"):
        saleae.SaleaeLogic()


@pytest.mark.parametrize("stage", ["initialization", "context"])
def test_ALX1753_P31_disconnect_failure_preserves_the_original_error(bench, stage):
    manager, _ = bench()
    original = RuntimeError("original operation failed")
    manager.close_failure = RuntimeError("disconnect failed")

    def fail():
        if stage == "initialization":
            manager.failure = original
            saleae.SaleaeLogic()
        else:
            with saleae.SaleaeLogic():
                raise original

    with pytest.raises(RuntimeError) as result:
        fail()
    assert result.value is original
    assert manager.close_count == 1
    assert any("disconnect failed" in note for note in original.__notes__)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": " "},
        {"device_id": ""},
        {"port": True},
        {"port": 65536},
        {"timeout_s": 0},
        {"timeout_s": float("nan")},
    ],
)
def test_ALX1753_P32_invalid_connection_arguments_do_not_connect(bench, kwargs):
    manager, connected = bench()
    with pytest.raises(InstrumentError):
        saleae.SaleaeLogic(**kwargs)
    assert connected == []
    assert manager.close_count == 0


def rpc_failure(status=grpc.StatusCode.UNAVAILABLE):
    error = grpc.RpcError("fake connection failure")
    error.code = lambda: status
    return error


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_ALX1753_P33_unavailable_local_server_launches_once(bench, monkeypatch, host):
    manager, connected = bench()
    manager.connect_failure = rpc_failure()
    checked = []
    application = Path("vendor") / "Logic.exe"

    def not_running(path):
        checked.append(path)
        return False

    monkeypatch.setattr(saleae, "_logic_running", not_running)
    with saleae.SaleaeLogic(
        host=host, port=10431, application_path=application, launch_timeout_s=30.0
    ) as logic:
        assert logic.launched is True
        assert logic.identity is manager.devices[0]
    assert len(connected) == 1
    assert len(checked) == 1
    assert str(checked[0]) == str(application)
    assert manager.launches == [
        {"application_path": application, "port": 10431, "connect_timeout_seconds": 30.0}
    ]
    assert manager.close_count == 1
    assert manager.started == []


def test_ALX1753_P34_running_application_without_server_is_not_launched_again(bench, monkeypatch):
    manager, _ = bench()
    manager.connect_failure = rpc_failure()
    monkeypatch.setattr(saleae, "_logic_running", lambda path: True)
    with pytest.raises(InstrumentError, match=r"(?i)automation|server"):
        saleae.SaleaeLogic()
    assert manager.launches == []


@pytest.mark.parametrize(
    ("kwargs", "failure"),
    [
        ({"auto_launch": False}, rpc_failure()),
        ({"host": "logic.invalid"}, rpc_failure()),
        ({}, rpc_failure(grpc.StatusCode.PERMISSION_DENIED)),
        ({}, RuntimeError("other native error")),
    ],
)
def test_ALX1753_P35_other_connection_failures_never_launch(bench, monkeypatch, kwargs, failure):
    manager, _ = bench()
    manager.connect_failure = failure
    inspected = []

    def not_running(path):
        inspected.append(path)
        return False

    monkeypatch.setattr(saleae, "_logic_running", not_running)
    with pytest.raises(type(failure)) as result:
        saleae.SaleaeLogic(**kwargs)
    assert result.value is failure
    assert inspected == []
    assert manager.launches == []


def test_ALX1753_P36_device_discovery_failure_does_not_launch_a_second_app(bench):
    manager, _ = bench()
    failure = rpc_failure()
    manager.failure = failure
    with pytest.raises(grpc.RpcError) as result:
        saleae.SaleaeLogic()
    assert result.value is failure
    assert manager.launches == []
    assert manager.close_count == 1


def test_ALX1753_P37_process_enumeration_error_does_not_authorize_launch(bench, monkeypatch):
    manager, _ = bench()
    manager.connect_failure = rpc_failure()
    failure = PermissionError("process inventory unavailable")

    def unavailable(path):
        raise failure

    monkeypatch.setattr(saleae, "_logic_running", unavailable)
    with pytest.raises(PermissionError) as result:
        saleae.SaleaeLogic()
    assert result.value is failure
    assert manager.launches == []


@pytest.mark.parametrize(
    "kwargs", [{"auto_launch": 1}, {"launch_timeout_s": 0}, {"launch_timeout_s": float("nan")}]
)
def test_ALX1753_P38_invalid_launch_options_are_rejected_before_connect(bench, kwargs):
    manager, connected = bench()
    with pytest.raises(InstrumentError):
        saleae.SaleaeLogic(**kwargs)
    assert connected == []
    assert manager.launches == []


@pytest.mark.parametrize(
    ("names", "application", "expected"),
    [
        (["LOGIC.EXE"], None, True),
        (["Logic2.exe"], None, True),
        (["logic"], None, True),
        (["logic2"], None, True),
        (["CustomLogic.exe"], Path("vendor") / "CustomLogic.exe", True),
        ([None, "python.exe"], None, False),
        ([], None, False),
    ],
)
def test_ALX1753_P39_process_names_detect_existing_default_or_custom_application(
    monkeypatch, names, application, expected
):
    requested = []

    def process_iter(fields):
        requested.append(fields)
        return [SimpleNamespace(info={"name": name}) for name in names]

    def module(name):
        assert name == "psutil"
        return SimpleNamespace(process_iter=process_iter)

    monkeypatch.setattr(importlib, "import_module", module)
    assert saleae._logic_running(application) is expected
    assert requested == [["name"]]


def test_ALX1753_P40_native_launch_failure_is_preserved_without_another_attempt(bench, monkeypatch):
    manager, _ = bench()
    manager.connect_failure = rpc_failure()
    failure = RuntimeError("native launch failed")
    manager.launch_failure = failure
    monkeypatch.setattr(saleae, "_logic_running", lambda path: False)
    with pytest.raises(RuntimeError) as result:
        saleae.SaleaeLogic()
    assert result.value is failure
    assert len(manager.launches) == 1
    assert manager.close_count == 0
