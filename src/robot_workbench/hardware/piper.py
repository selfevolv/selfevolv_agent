"""复用 RLinf 的 PiperArm，以统一的 pyAgxArm 通路提供显式使能、受检查控制和反馈读取。"""
from __future__ import annotations

import math
import time
from types import SimpleNamespace
import numpy as np
from rlinf.robotics.parts.arms.piper import PiperArm
from robot_workbench.core.config import load_config
from robot_workbench.hardware.state import ArmCommand, GripperSample, JointSample, SafetyStop


class PiperArmController(PiperArm):
    """继承 RLinf 的连接和运动实现，补充显式使能、反馈校验及实际目标记录。"""
    def __init__(self, channel, *, feedback_timeout=.3, gripper_max_width=.1, role_setup=False, **kwargs):
        """初始化控制器；反馈超时为秒、夹爪最大宽度为米，未显式指定固件时读取配置。"""
        if 'firmware' not in kwargs:
            kwargs['firmware'] = load_config().can_firmware.get(channel)
        super().__init__(channel, gripper_max_width=gripper_max_width, **kwargs)
        self.role_setup = role_setup
        self.feedback_timeout = feedback_timeout
        self.armed = False
        self.last_command = None
        self._speed = None
        self._leader_role = False

    def _enable(self, robot):
        # RLinf 连接流程会调用此钩子；此处保留显式使能边界。
        """覆盖连接阶段的自动使能钩子；连接时不使能，实际使能由 enable 显式触发。"""
        pass

    def _await_feedback(self, robot):
        """常规连接等待首帧关节反馈；角色配置连接跳过该等待。"""
        if not self.role_setup:
            super()._await_feedback(robot)

    def _release(self, device):
        """清理本地控制状态并交由父类断开连接和释放设备锁，不主动失能。"""
        self.armed = False
        self.last_command = None
        self._speed = None
        super()._release(device)

    @property
    def joint_limits(self):
        """返回六轴关节的弧度限位，形状为 (6, 2)，两列分别为下限和上限。"""
        return np.column_stack((self._limits_lower, self._limits_upper))

    def joint_limit(self, name):
        """按 j1 至 j6 的关节名称返回该轴弧度限位。"""
        return tuple(self.joint_limits[int(name.removeprefix('j')) - 1])

    def _reading(self, value, label):
        """按 Unix 秒时间戳检查反馈存在且未超时；缺失、未来或过期反馈抛出 SafetyStop。"""
        if value is None:
            raise SafetyStop(f'{self._channel} 未收到{label}反馈')
        stamp = float(value.timestamp)
        age = time.time() - stamp
        if not math.isfinite(stamp) or not 0 <= age <= self.feedback_timeout:
            raise SafetyStop(f'{self._channel} {label}反馈已过期：age={age:.3f}s')
        return value

    def read_joints(self):
        # 固定版本 SDK 会将缺失的关节对补零，因此必须逐组检查原始反馈。
        """校验三组关节帧完整且新鲜，返回以毫度保存、可转为弧度的六轴反馈。"""
        for pair in ('joint_12', 'joint_34', 'joint_56'):
            self._reading(getattr(self._robot._parser, pair, None), pair)
        value = self._reading(self._robot.get_joint_angles(), '六轴关节')
        joints = np.asarray(value.msg, dtype=float)
        if joints.shape != (6,) or not np.isfinite(joints).all():
            raise SafetyStop(f'{self._channel} 六轴关节反馈无效：{joints}')
        return JointSample(tuple(np.rint(np.rad2deg(joints) * 1000).astype(int)),
                           float(value.timestamp), float(value.hz))

    def read_gripper(self):
        """校验夹爪宽度模式及反馈时效，返回微米宽度、mN 夹持力和使能状态。"""
        value = self._reading(self._gripper.get_gripper_status(), '夹爪')
        state = value.msg
        if state.mode != 'width' or not np.isfinite([state.value, state.force]).all():
            raise SafetyStop(f'{self._channel} 夹爪必须上报有效宽度和夹持力')
        return GripperSample(round(state.value * 1e6), round(state.force * 1000),
                             float(value.timestamp), float(value.hz), bool(state.foc_status.driver_enable_status))

    def read_leader_input(self, after_timestamp=0.):
        """校验主臂关节与夹爪输入；输入时间戳须晚于指定 Unix 秒时间。"""
        for pair in ('leader_joint_12', 'leader_joint_34', 'leader_joint_56'):
            value = self._reading(getattr(self._robot._parser, pair, None), pair)
            if value.timestamp <= after_timestamp:
                raise SafetyStop(f'{self._channel} 尚未收到切换后的完整主臂输入')
        joints = self._reading(self._robot.get_leader_joint_angles(), '主臂关节输入')
        grip = self._reading(self._gripper.get_gripper_ctrl_states(), '主臂夹爪输入')
        if grip.timestamp <= after_timestamp:
            raise SafetyStop(f'{self._channel} 尚未收到切换后的主臂夹爪输入')
        angles = np.asarray(joints.msg, dtype=float)
        if angles.shape != (6,) or not np.isfinite(angles).all():
            raise SafetyStop(f'{self._channel} 主臂六轴输入无效')
        if grip.msg.status_code not in (0, 1, 2, 3) or not np.isfinite([grip.msg.value, grip.msg.force]).all():
            raise SafetyStop(f'{self._channel} 主臂夹爪输入必须为有效宽度和夹持力')
        return (JointSample(tuple(np.rint(np.rad2deg(angles) * 1000).astype(int)),
                            float(joints.timestamp), float(joints.hz)),
                GripperSample(round(grip.msg.value * 1e6), round(grip.msg.force * 1000),
                              float(grip.timestamp), float(grip.hz), False))

    def enter_leader(self):
        """请求 SDK 切换到示教输入臂角色并清除本地运动状态；切换效果须由新输入帧确认。"""
        self.assert_healthy()
        self._robot.set_leader_mode()
        self._leader_role = True
        self.armed = False
        self.last_command = None

    def restore_can_control(self, timeout_seconds=5.0):
        """切换到输出角色和关节模式，显式使能并等待新反馈确认；超时单位为秒。"""
        requested_at = time.time()
        self.armed = False
        self.last_command = None
        if not self._leader_role:
            self.assert_healthy()
        self.set_output_role()
        self._robot.set_motion_mode(self._robot.OPTIONS.MOTION_MODE.J)
        time.sleep(.005)
        super()._enable(self._robot)
        self.wait_for_can_control(requested_at, timeout_seconds, transitioning=True)
        self.armed = True
        self._leader_role = False

    def status(self):
        """读取新鲜整机状态并保留原始协议码；时间戳为 Unix 秒、反馈频率为 Hz。"""
        value = self._reading(self._robot.get_arm_status(), '整机状态')
        state = value.msg
        return SimpleNamespace(timestamp=float(value.timestamp), hz=float(value.hz),
                               control_mode=int(state.ctrl_mode), move_mode=int(state.mode_feedback),
                               teach_status=int(state.teach_status),
                               arm_status=int(state.arm_status), error_code=int(state.err_code))

    def select_joint_mode(self):
        """检查设备健康后选择关节运动模式，不下发关节目标。"""
        self.assert_healthy()
        self._robot.set_motion_mode(self._robot.OPTIONS.MOTION_MODE.J)
        # pyAgxArm #63 建议在模式选择与首个关节目标之间保留间隔。
        time.sleep(.005)

    def enter_teaching(self, timeout_seconds=5.0):
        """发送进入拖动示教请求，并在指定秒数内等待新状态帧确认。"""
        from pyAgxArm.protocols.can_protocol.msgs.piper.default.transmit import ArmMsgMotionCtrl
        initial = latest = self.status()
        def matches(state):
            """判断状态是否同时上报拖动示教控制模式和示教开启标记。"""
            return state.control_mode == 2 and state.teach_status == 1
        if matches(initial):
            return
        self.assert_healthy(teaching=True)
        self._robot._send_msg(ArmMsgMotionCtrl(grag_teach_ctrl=1))

        def ended():
            """校验设备健康，并判断新于初始反馈的状态帧是否确认示教开启。"""
            nonlocal latest
            self.assert_healthy(teaching=True)
            latest = self.status()
            return latest.timestamp > initial.timestamp and matches(latest)

        action = '进入拖动示教'
        if not self._robot._ctx._wait_for_response(ended, timeout=timeout_seconds):
            raise SafetyStop(f'{self._channel} {action}未确认：control_mode={latest.control_mode}, '
                             f'teach_status={latest.teach_status}')
        print(f'{self._channel} 已确认{action}：control_mode={latest.control_mode}, '
              f'teach_status={latest.teach_status}', flush=True)
        self.armed = False
        self.last_command = None

    def wait_for_can_control(self, after_timestamp, timeout_seconds=5.0, *, transitioning=False):
        """在指定秒数内等待晚于给定 Unix 时间的 CAN 关节控制及六轴使能反馈。"""
        latest = None
        def ready():
            """检查驱动健康及新状态帧；切换期间允许继续等待尚未到达的状态或使能反馈。"""
            nonlocal latest
            try:
                self.enabled_joints()
            except SafetyStop:
                if transitioning:
                    return False
                raise
            self.assert_drivers_healthy()
            try:
                latest = self.status()
            except SafetyStop:
                if transitioning:
                    return False
                raise
            if not transitioning or latest.timestamp > after_timestamp:
                self.assert_healthy()
            return (latest.timestamp > after_timestamp and latest.control_mode == 1
                    and latest.move_mode == 1 and latest.teach_status != 1 and all(self.enabled_joints()))
        if not self._robot._ctx._wait_for_response(ready, timeout=timeout_seconds):
            raise SafetyStop(f'{self._channel} CAN 控制未确认：{latest}')

    def enabled_joints(self):
        # SDK 对缺失驱动帧返回 False，因此还需检查反馈是否实际存在且新鲜。
        """读取六轴驱动的新鲜反馈，按 J1 至 J6 返回使能布尔值。"""
        return [bool(self._reading(self._robot.get_driver_states(i), f'J{i}驱动器').msg.foc_status.driver_enable_status)
                for i in range(1, 7)]

    def assert_healthy(self, *, damping=False, teaching=False):
        """按普通、阻尼或示教场景检查整机状态，并拒绝整机错误或驱动故障。"""
        state = self.status()
        expected = (0, 11) if teaching else (1,) if damping else (0,)
        if state.error_code or state.arm_status not in expected:
            raise SafetyStop(f'{self._channel} 状态异常：state={state.arm_status}, error=0x{state.error_code:04X}')
        self.assert_drivers_healthy()

    def assert_drivers_healthy(self):
        """检查六轴驱动反馈时效及电压、温度、过流、碰撞等故障标记。"""
        for i in range(1, 7):
            status = self._reading(self._robot.get_driver_states(i), f'J{i}驱动器').msg.foc_status
            faults = [name for name in ('voltage_too_low', 'motor_overheating', 'driver_overcurrent',
                      'driver_overheating', 'collision_status', 'driver_error_status', 'stall_status')
                      if getattr(status, name, False)]
            if faults:
                raise SafetyStop(f'{self._channel} J{i} 驱动器故障：{faults}')

    def set_output_role(self):
        """通过 SDK 请求设置运动输出臂角色，不使能或下发运动目标。"""
        self._robot.set_follower_mode()

    def set_speed(self, percent):
        """校验并设置 1 至 100 的速度百分比；与缓存相同时不重复发送。"""
        if type(percent) not in (int, float) or not math.isfinite(percent) or not 1 <= percent <= 100:
            raise ValueError('机械臂速度必须为1..100%')
        if self._speed != percent:
            self._robot.set_speed_percent(int(percent))
            self._speed = percent

    def enable(self):
        """检查健康和关节反馈后显式使能；已进入控制但使能丢失时拒绝自动恢复。"""
        self.assert_healthy()
        if self.armed:
            if not all(self.enabled_joints()):
                raise SafetyStop(f'{self._channel} 控制期间使能丢失')
            return
        self.read_joints()
        super()._enable(self._robot)
        self.armed = True

    def disable(self):
        # 仅在已托稳后的显式释放流程中调用，断开连接不会调用此方法。
        """显式请求机械臂失能，成功时清除本地使能标记；操作前须物理托稳。"""
        if not self._robot.disable():
            return False
        self.armed = False
        return True

    def command(self, joints_rad=None, gripper_width_m=None, force_n=None):
        """校验健康与六轴使能后下发目标：六轴弧度、夹爪宽度米、夹持力 0 至 3 N。

        关节目标由父类按限位裁剪，夹爪宽度裁剪到有效行程；返回记录的实际目标，未指定的运动分量不下发。"""
        self.assert_healthy()
        if not self.armed or not all(self.enabled_joints()):
            raise SafetyStop(f'{self._channel} 未使能或控制期间使能丢失，拒绝运动')
        joints = None if joints_rad is None else np.asarray(joints_rad, dtype=float)
        if joints is not None and (joints.shape != (6,) or not np.isfinite(joints).all()):
            raise ValueError('关节目标必须为6维有限弧度数值')
        if gripper_width_m is not None:
            if not math.isfinite(gripper_width_m):
                raise ValueError('夹爪目标必须为有限数值')
            gripper_width_m = float(np.clip(gripper_width_m, 0., self._gripper_max_width))
        if force_n is not None and (not math.isfinite(force_n) or not 0 <= force_n <= 3):
            raise ValueError('夹持力必须为0..3N')
        self._gripper_force = self._gripper_force if force_n is None else force_n
        sent = super().move_joints(joints) if joints is not None else (
            self.last_command.joints_rad.copy() if self.last_command else self.read_joints().radians)
        if gripper_width_m is not None:
            super().move_gripper([gripper_width_m / self._gripper_max_width])
        width = gripper_width_m if gripper_width_m is not None else (
            self.last_command.gripper_width_m if self.last_command else None)
        self.last_command = ArmCommand(sent.copy(), width, self._gripper_force, time.perf_counter_ns())
        return self.last_command

    def command_sample(self, values, gripper_angle=None, effort=None):
        """将历史格式的毫度关节、微米夹爪宽度和 mN 力转换为 SI 单位后下发。"""
        return self.command(np.deg2rad(np.asarray(values, dtype=float) / 1000),
                            None if gripper_angle is None else gripper_angle * 1e-6,
                            None if effort is None else effort * .001)

    def send_joint_targets(self, *values):
        """将六轴毫度目标经统一校验入口下发。"""
        return self.command_sample(values)

    def send_gripper_target(self, width_um, force_mn):
        """将微米宽度与 mN 夹持力转换为米和牛顿后下发。"""
        return self.command(gripper_width_m=width_um * 1e-6, force_n=force_mn * .001)

    def hold(self):
        """校验实测关节与夹爪未越界后发送一次原位保持目标，不启动持续保持循环。"""
        joints, grip = self.read_joints(), self.read_gripper()
        if (np.any(joints.radians < self.joint_limits[:, 0]) or
                np.any(joints.radians > self.joint_limits[:, 1]) or
                not 0 <= grip.width_m <= self._gripper_max_width):
            raise SafetyStop(f'{self._channel} 实测姿态超出 SDK 范围，不能裁剪后宣称原位保持')
        return self.command(joints.radians, grip.width_m)

    def enter_damping(self):
        # 沿用现有主臂的阻尼急停方式，通过 SDK 发送。
        """发送 SDK 电子急停请求，并在五秒内确认主臂进入阻尼状态。"""
        self._robot.electronic_emergency_stop()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self.assert_healthy(damping=True)
                return
            except SafetyStop:
                time.sleep(.05)
        raise SafetyStop(f'{self._channel} 未确认主臂阻尼状态')

    def release_gripper(self):
        """请求主臂夹爪失能并在五秒内等待 SDK 成功返回，允许后续手动开合。"""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._gripper.disable_gripper():
                return
            time.sleep(.05)
        raise SafetyStop(f'{self._channel} 未确认主臂夹爪失能')

    def motor_samples(self):
        """按 J1 至 J6 返回经时效校验的电机高速反馈样本。"""
        return [self._reading(self._robot.get_motor_states(i), f'J{i}电机高速') for i in range(1, 7)]
