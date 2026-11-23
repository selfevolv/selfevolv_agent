"""定义各相机后端共用的不可变彩色帧与深度帧数据对象。

彩色帧携带图像、主机单调时钟时间戳和序号；深度帧保留毫米单位原始值，
供独立诊断测试使用。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CameraFrame:
    """保存 BGR 图像、主机单调时钟纳秒时间戳和帧序号。"""

    image: np.ndarray
    timestamp_ns: int
    sequence: int


@dataclass(frozen=True)
class DepthFrame:
    """保存 uint16 毫米深度、单调时钟纳秒、帧序号及原始米制深度比例。"""

    depth_mm: np.ndarray
    timestamp_ns: int
    sequence: int
    depth_scale_m: float
