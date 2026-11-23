"""定义与相机型号无关的线程化彩色流接口。

统一启动、停止、最新帧复制、预热及读取错误处理；具体 V4L2/RealSense 采帧
由 models/ 实现，采集程序只依赖这个抽象接口。
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from robot_workbench.hardware.cameras.frames import CameraFrame, DepthFrame


class CameraError(RuntimeError):
    """表示相机未发现、启动失败、取帧中断或资源释放异常。"""


class UnsupportedDepthError(CameraError):
    """表示当前相机后端不提供毫米单位深度图。"""


class ThreadedColorCamera(ABC):
    """在后台线程中预热并持续读取最新 BGR 彩色帧。"""

    backend_name = "unknown"
    model_name = "unknown"

    def __init__(
        self,
        name: str,
        serial: str,
        width: int,
        height: int,
        fps: float,
        warmup_frames: int,
    ) -> None:
        """初始化配置及内部状态，不启动设备或网络请求。"""
        self.name = name
        self.serial = serial
        self.width = width
        self.height = height
        self.fps = fps
        self.warmup_frames = warmup_frames
        self._frame: CameraFrame | None = None
        self._error: BaseException | None = None
        self._configured: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None

    @abstractmethod
    def probe_info(self) -> dict[str, Any]:
        """读取相机设备信息，不启动图像采集流。"""

    @abstractmethod
    def _open(self) -> dict[str, Any]:
        """打开相机采集流并返回实际生效的配置。"""

    @abstractmethod
    def _read_color(self) -> np.ndarray:
        """等待并读取一帧 BGR 彩色图像，失败时抛出相机错误。"""

    @abstractmethod
    def _close(self) -> None:
        """关闭当前后端占用的采集资源。"""

    def capture_depth(self) -> DepthFrame:
        """独立采集一帧毫米单位深度图；不支持深度或已启动彩色流时拒绝。"""
        raise UnsupportedDepthError(f"{self.model_name} 不支持深度图")

    def start(self) -> None:
        """启动后台采流；双相机管理器还会等待两路就绪，失败时清理已启动资源。"""
        if self._thread is not None:
            raise CameraError(f"{self.name} 相机不能重复启动")
        self._thread = threading.Thread(
            target=self._loop,
            name=f"camera-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def _loop(self) -> None:
        """执行后台打开、预热和最新帧更新，并在异常或停止时清理后端。"""
        try:
            configured = self._open()
            self._configured = {
                "name": self.name,
                "backend": self.backend_name,
                "model": self.model_name,
                "serial": self.serial,
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "warmup_frames": self.warmup_frames,
                **configured,
            }
            sequence = 0
            warmup_left = self.warmup_frames
            while not self._stop_event.is_set():
                image = self._read_color()
                if image is None or image.size == 0:
                    raise CameraError("读取到空的彩色帧")
                timestamp_ns = time.perf_counter_ns()
                if warmup_left > 0:
                    warmup_left -= 1
                    continue
                with self._lock:
                    self._frame = CameraFrame(image, timestamp_ns, sequence)
                sequence += 1
                self._ready_event.set()
        except BaseException as exc:
            self._error = exc
            self._ready_event.set()
        finally:
            try:
                self._close()
            except BaseException as exc:
                if self._error is None:
                    self._error = exc
                    self._ready_event.set()

    def wait_ready(self, timeout: float = 15.0) -> None:
        """在指定秒数内等待预热后首帧；超时或后台异常时抛出相机错误。"""
        if not self._ready_event.wait(timeout):
            raise CameraError(f"{self.name} 相机启动超时")
        if self._error is not None:
            raise CameraError(f"{self.name} 相机启动失败：{self._error}") from self._error

    def snapshot(self) -> CameraFrame:
        """返回最新 BGR 帧的独立图像副本，附带单调时钟纳秒和帧序号。"""
        if self._error is not None:
            raise CameraError(f"{self.name} 相机中断：{self._error}") from self._error
        with self._lock:
            if self._frame is None:
                raise CameraError(f"{self.name} 相机暂无图像")
            frame = self._frame
            return CameraFrame(frame.image.copy(), frame.timestamp_ns, frame.sequence)

    def config(self) -> dict[str, Any]:
        """返回已生效的相机流配置；未配置的相机抛出相机错误。"""
        if not self._configured:
            raise CameraError(f"{self.name} 相机尚未完成配置")
        return dict(self._configured)

    def close(self) -> None:
        """停止本对象的采流并释放资源；线程未退出或释放失败时报告错误。"""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=6.0)
            if self._thread.is_alive():
                raise CameraError(f"{self.name} 相机线程未能及时退出")

    def __enter__(self) -> "ThreadedColorCamera":
        """进入上下文时启动采流并等待首帧就绪。"""
        self.start()
        self.wait_ready()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        """离开上下文时关闭相机并释放资源。"""
        self.close()
