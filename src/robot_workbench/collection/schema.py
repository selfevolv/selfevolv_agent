"""定义 Piper 状态、动作、单位和人工结果标记的数据约定。"""
import numpy as np

STATE_NAMES = [
    *[f"joint_{i}.position_rad" for i in range(1, 7)],
    *[f"joint_{i}.velocity_rad_s" for i in range(1, 7)],
    *[f"joint_{i}.torque_nm" for i in range(1, 7)],
    *[f"joint_{i}.current_a" for i in range(1, 7)],
    "gripper.width_m", "gripper.force_n",
]
ACTION_NAMES = [
    *[f"joint_{i}.target_position_rad" for i in range(1, 7)],
    "gripper.target_width_m", "gripper.target_force_n",
]

SUCCESS_FEATURES = {
    name: {"dtype": "bool", "shape": (1,), "names": None}
    for name in ("is_success", "is_success_valid")
}
INTERVENTION_FEATURES = {
    'intervene_flag': {'dtype': 'bool', 'shape': (1,), 'names': None},
}


def recorded_success(arrays):
    """读取最后一帧的有效人工结果；缺少完整有效标记时返回 None，不将未知结果视为失败。"""
    if arrays is None or "is_success" not in arrays or "is_success_valid" not in arrays:
        return None
    success = np.asarray(arrays["is_success"])
    valid = np.asarray(arrays["is_success_valid"])
    if (success.ndim != 1 or not success.size or success.shape != valid.shape or
            success.dtype != np.bool_ or valid.dtype != np.bool_ or not valid.all()):
        return None
    return bool(success[-1])


def validate_episode(metadata, arrays, features, fps):
    """校验片段字段、采样率、数组、物理单位和人工标记，拒绝混入不兼容数据。"""
    for key, field in (("state_names", "observation.state"), ("action_names", "action")):
        if metadata[key] != features[field]["names"]:
            raise ValueError(f"采集字段含义或顺序不一致：{key}")
    if float(metadata["fps"]) != fps:
        raise ValueError(f"原始 episode 采样率不一致：{metadata['fps']} != {fps}")
    count = len(arrays["elapsed_s"])
    if 'intervene_flag' in features:
        if metadata.get('intervention_schema') != 'rlinf.intervene_flag.v1':
            raise ValueError('干预标记格式不同，请使用独立数据集')
        flags = np.asarray(arrays['intervene_flag'])
        if flags.shape != (count,) or flags.dtype != np.bool_:
            raise ValueError('逐帧干预标记必须为与动作对齐的布尔数组')
    for key, field in (("observation_state", "observation.state"), ("action", "action")):
        values = arrays[key]
        if values.shape != (count, features[field]["shape"][0]) or not np.isfinite(values).all():
            raise ValueError(f"采集数组维度错误或包含无效数值：{key}")
    units = metadata.get("units", {})
    expected = {"joint_position": "rad", "joint_velocity": "rad/s", "joint_torque": "N.m",
                "joint_current": "A", "gripper_width": "m", "gripper_torque": "N.m"}
    if metadata.get('schema_version', 1) >= 2:
        expected.pop('gripper_torque')
        expected['gripper_force'] = 'N'
        if units.get('gripper_force') != 'N':
            raise ValueError('pyAgxArm 夹爪反馈必须明确标注为 N')
    if any(units.get(key, value) != value for key, value in expected.items()):
        raise ValueError("采集数据单位不一致")
    if "is_success" in features:
        if "is_success" not in metadata or (metadata["is_success"] is not None and type(metadata["is_success"]) is not bool):
            raise ValueError("缺少有效的人工任务结果；不自动补标旧数据")
        outcome = metadata["is_success"]
        for key, expected_value in (("is_success", outcome is True), ("is_success_valid", outcome is not None)):
            if key not in arrays:
                raise ValueError(f"缺少任务结果数组：{key}")
            values = np.asarray(arrays[key])
            if values.shape != (count,) or values.dtype != np.bool_ or not np.all(values == expected_value):
                raise ValueError(f"任务结果与人工标注不一致：{key}")


def pyagx_repo_id(repo_id):
    """为采用夹持力语义的数据集添加后缀，避免与旧力矩数据混用。"""
    return repo_id if repo_id.endswith('_pyagx') else repo_id + '_pyagx'


OUTCOME_KEYS = {'s': True, 'f': False, 'u': None, 'y': None}


def outcome_arrays(outcome, count):
    """将成功、失败或未知结果生成指定帧数的布尔结果数组及有效性掩码。"""
    if outcome is not None and type(outcome) is not bool:
        raise ValueError('任务结果必须是成功、失败或未确认')
    return {'is_success':np.full(count,outcome is True,dtype=bool),
            'is_success_valid':np.full(count,outcome is not None,dtype=bool)}
