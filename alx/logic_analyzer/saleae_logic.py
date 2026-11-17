# SPDX-License-Identifier: MIT
"""Saleae Logic devices through the official Logic 2 automation API.

Install the ``saleae`` extra. This adapter connects to a running application or
launches Logic 2 locally when it is not running. Existing sessions must have
their automation server enabled. Closing disconnects an existing application,
or closes the application launched by this adapter.
Selection requires a real Logic 8, Logic Pro 8 or Logic Pro 16. Pass a device ID
when multiple devices are attached. Serial numbers and channel wiring belong
in the consuming bench configuration.

``start_capture`` returns Saleae's native Capture: call ``wait()`` once, then
save/export and ``close()`` before closing this adapter. The caller owns that
capture, including cleanup on failure. Existing UI captures are not touched.
``timeout_s`` is the SDK's connection retry setting, not a deadline for capture,
save/export or other RPCs. Use the adapter from one thread at a time.

https://saleae.github.io/logic2-automation/automation.html
"""

from __future__ import annotations

import importlib
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING

from alx.errors import InstrumentError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType, TracebackType

    from saleae import automation


log = logging.getLogger(__name__)


def _automation() -> ModuleType:
    try:
        return importlib.import_module("saleae.automation")
    except ModuleNotFoundError as exc:
        if exc.name not in ("saleae", "saleae.automation"):
            raise
        raise InstrumentError(
            "Saleae support requires the saleae extra: uv sync --extra saleae"
        ) from exc


def _logic_running(application_path: str | Path | None) -> bool:
    psutil = importlib.import_module("psutil")
    names = {"logic", "logic.exe", "logic2", "logic2.exe"}
    if application_path is not None:
        names.add(Path(application_path).name.casefold())
    return any(
        (process.info["name"] or "").casefold() in names
        for process in psutil.process_iter(["name"])
    )


class SaleaeLogic:
    """Connect or launch Logic 2, select one physical analyzer and start timed captures."""

    def __init__(
        self,
        device_id: str | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 10430,
        timeout_s: float = 5.0,
        name: str = "Logic",
        auto_launch: bool = True,
        application_path: str | Path | None = None,
        launch_timeout_s: float = 20.0,
    ) -> None:
        """Connect first, or launch a local app if absent; identify without starting a capture.

        Set auto_launch=False for connection only. application_path overrides
        the SDK's Windows install discovery and is required on Linux/macOS.
        Both timeout values are SDK connection retry periods, not RPC deadlines.
        """
        self._nonempty(host, "host")
        if type(port) is not int or not 1 <= port <= 65535:
            raise InstrumentError("port must be an integer in 1..65535")
        self._positive_number(timeout_s, "timeout_s")
        self._positive_number(launch_timeout_s, "launch_timeout_s")
        if type(auto_launch) is not bool:
            raise InstrumentError("auto_launch must be a bool")
        if application_path is not None:
            self._nonempty(str(application_path), "application_path")
        if device_id is not None:
            self._nonempty(device_id, "device_id")
        self.name = name
        self._api = _automation()
        self.launched = False
        self._manager: automation.Manager | None = self._connect(
            host, port, timeout_s, auto_launch, application_path, launch_timeout_s
        )
        try:
            manager = self._connected()
            devices = [
                device
                for device in manager.get_devices(include_simulation_devices=False)
                if not device.is_simulation
            ]
            matches = [device for device in devices if device_id in (None, device.device_id)]
            if len(matches) != 1:
                raise InstrumentError(
                    f"Expected one physical analyzer, found {len(matches)} matching devices; "
                    "check USB connection and supply device_id if needed"
                )
            self.identity: automation.DeviceDesc = matches[0]
            supported = {
                self._api.DeviceType.LOGIC_8: 8,
                self._api.DeviceType.LOGIC_PRO_8: 8,
                self._api.DeviceType.LOGIC_PRO_16: 16,
            }
            if self.identity.device_type not in supported:
                raise InstrumentError(f"Unsupported device: {self.identity.device_type}")
            self._channel_count = supported[self.identity.device_type]
            self.app_info: automation.AppInfo = manager.get_app_info()
        except BaseException as exc:
            self._close_after_error(exc)
            raise

    def _connect(
        self,
        host: str,
        port: int,
        timeout_s: float,
        auto_launch: bool,
        application_path: str | Path | None,
        launch_timeout_s: float,
    ) -> automation.Manager:
        grpc = importlib.import_module("grpc")
        try:
            return self._api.Manager.connect(
                address=host, port=port, connect_timeout_seconds=timeout_s
            )
        except grpc.RpcError as exc:
            if (
                exc.code() != grpc.StatusCode.UNAVAILABLE
                or not auto_launch
                or host.casefold() not in ("127.0.0.1", "localhost", "::1")
            ):
                raise
            if _logic_running(application_path):
                raise InstrumentError(
                    f"Logic 2 is already running but its API is unavailable at {host}:{port}; "
                    "enable Automation Server and check its port"
                ) from exc
        manager = self._api.Manager.launch(
            application_path=application_path,
            port=port,
            connect_timeout_seconds=launch_timeout_s,
        )
        self.launched = True
        return manager

    def _connected(self) -> automation.Manager:
        if self._manager is None:
            raise InstrumentError("Logic 2 connection is closed; open a new adapter")
        return self._manager

    @staticmethod
    def _nonempty(value: object, field: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise InstrumentError(f"{field} must be nonempty")

    @staticmethod
    def _positive_number(value: float, field: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InstrumentError(f"{field} must be a finite positive number")
        try:
            valid = math.isfinite(value) and value > 0
        except OverflowError:
            valid = False
        if not valid:
            raise InstrumentError(f"{field} must be a finite positive number")

    def _channels(self, channels: Sequence[int]) -> list[int]:
        selected = list(channels)
        if any(type(ch) is not int or not 0 <= ch < self._channel_count for ch in selected):
            raise InstrumentError(
                f"Channel indexes must be integers in 0..{self._channel_count - 1}"
            )
        if len(set(selected)) != len(selected):
            raise InstrumentError("Channel indexes must be unique within each channel group")
        return selected

    def start_capture(
        self,
        *,
        duration_s: float,
        digital_channels: Sequence[int],
        digital_sample_rate: int,
        digital_threshold_volts: float | None = None,
        analog_channels: Sequence[int] = (),
        analog_sample_rate: int | None = None,
    ) -> automation.Capture:
        """Start a timed capture and return its native, caller-owned Saleae handle.

        Digital capture is required; analog channels are optional. Rates are in
        samples/second; Logic 2 validates the device/USB/channel combinations.
        Pro models require a logic standard of 1.2, 1.8 or 3.3 V (not the actual
        comparator threshold). Logic 8 uses its fixed standard: omit this value.
        """
        manager = self._connected()
        self._positive_number(duration_s, "duration_s")
        digital = self._channels(digital_channels)
        analog = self._channels(analog_channels)
        if not digital:
            raise InstrumentError("Select at least one digital channel")
        if type(digital_sample_rate) is not int or digital_sample_rate <= 0:
            raise InstrumentError("digital_sample_rate must be a positive integer")
        if analog:
            if type(analog_sample_rate) is not int or analog_sample_rate <= 0:
                raise InstrumentError("analog_sample_rate must be a positive integer")
        elif analog_sample_rate is not None:
            raise InstrumentError("analog_sample_rate requires analog channels")
        if isinstance(digital_threshold_volts, bool):
            raise InstrumentError("digital_threshold_volts must be a logic standard voltage")
        if self.identity.device_type == self._api.DeviceType.LOGIC_8:
            if digital_threshold_volts not in (None, 0):
                raise InstrumentError("Logic 8 has a fixed threshold; omit digital_threshold_volts")
        elif digital_threshold_volts not in (1.2, 1.8, 3.3):
            raise InstrumentError("Pro devices require digital_threshold_volts of 1.2, 1.8 or 3.3")
        return manager.start_capture(
            device_id=self.identity.device_id,
            device_configuration=self._api.LogicDeviceConfiguration(
                enabled_digital_channels=digital,
                enabled_analog_channels=analog,
                digital_sample_rate=digital_sample_rate,
                analog_sample_rate=analog_sample_rate,
                digital_threshold_volts=digital_threshold_volts,
            ),
            capture_configuration=self._api.CaptureConfiguration(
                capture_mode=self._api.TimedCaptureMode(duration_seconds=duration_s)
            ),
        )

    def close(self) -> None:
        """Disconnect once, closing only an app we launched; finish/close captures first."""
        manager, self._manager = self._manager, None
        if manager is not None:
            manager.close()

    def _close_after_error(self, error: BaseException) -> None:
        try:
            self.close()
        except Exception as close_error:
            log.exception("Logic 2 disconnect failed while preserving the original error")
            error.add_note(f"Logic 2 disconnect also failed: {close_error}")

    def __enter__(self) -> SaleaeLogic:
        """Use this connection in a with statement."""
        self._connected()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Disconnect, leaving an existing application open or closing our launched one."""
        if exc_value is None:
            self.close()
        else:
            self._close_after_error(exc_value)
