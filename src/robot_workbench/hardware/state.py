"""定义反馈样本与下发指令；设备控制统一使用弧度、米和牛顿。"""
from dataclasses import dataclass
import numpy as np

RAW_DEG_TO_RAD = np.pi / 180000.

class SafetyStop(RuntimeError):
    """表示反馈、设备状态或控制条件不满足继续操作的要求。"""
    pass

@dataclass(frozen=True)
class JointSample:
    # 保留毫度边界以兼容现有界面和片段序列化。
    """保存六轴关节反馈：位置为毫度，时间戳为 Unix 秒，反馈频率为 Hz。"""
    values: tuple[int, ...]
    timestamp: float
    hz: float

    @property
    def radians(self):
        """将六轴毫度反馈转换为弧度数组。"""
        return np.asarray(self.values, dtype=float) * RAW_DEG_TO_RAD

@dataclass(frozen=True)
class GripperSample:
    """保存夹爪反馈：宽度为微米、力为 mN、时间戳为 Unix 秒、频率为 Hz，另含使能状态。"""
    angle: int  # 现有界面与记录边界的宽度，单位为微米。
    effort: int  # 单位为 mN；数据格式 v2 将其记录为力，而非力矩。
    timestamp: float
    hz: float
    enabled: bool

    @property
    def width_m(self):
        """将夹爪反馈宽度从微米转换为米。"""
        return self.angle * 1e-6

    @property
    def force_n(self):
        """将夹爪反馈力从 mN 转换为牛顿。"""
        return self.effort * .001

@dataclass(frozen=True)
class ArmCommand:
    """保存实际下发目标：关节为弧度、夹爪宽度为米、力为牛顿，时间为单调时钟纳秒。"""
    joints_rad: np.ndarray
    gripper_width_m: float | None
    gripper_force_n: float | None
    timestamp_ns: int
