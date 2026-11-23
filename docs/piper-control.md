# Piper 控制代码提取

2026-11-23 从 `/home/youlika/code/piper_master_slave_data_collection` 原样提取控制核心；随后按用户要求，仅在本项目副本补充中文注释和文档字符串，执行逻辑保持一致，源项目未修改。逐文件来源、原始及当前注释版本的大小和 SHA-256 见 [来源清单](piper-source-manifest.json)。本次未执行 Git 操作，源 revision 未知。

## 选择范围

| 文件（相对于 `src/robot_workbench/`） | 必须保留的原因 |
| --- | --- |
| `hardware/piper.py` | `PiperArmController`，提供受检查的关节、夹爪控制及反馈 |
| `hardware/state.py` | 控制指令、反馈样本、单位转换和 `SafetyStop` |
| `core/config.py` | 控制类直接导入，默认构造时读取 CAN 固件配置 |
| `core/paths.py` | 配置模块的路径依赖 |
| `collection/schema.py` | 配置模块导入其中的 `pyagx_repo_id`；保持原文件，未裁剪成另一版本 |
| `hardware/can_interfaces.py` | 配置对象 `follower_interfaces()` 的延迟依赖 |
| 四个 `__init__.py` | 保持原包结构及 RLinf 的路径发现；其中三个提取时为空文件，现仅补充包用途说明 |

本地依赖关系：`piper → config → paths/schema/can_interfaces`，以及 `piper → state`。这些文件是保留原实现与包结构的依赖集合；没有复制采集执行器。

初次提取仅包含机械臂控制。本轮补入下述相机模块及 Creator OpenAI 接口，仍未引入 Web、任务管理、遥操作、主从跟随、数据采集、训练、旧 `piper_sdk`、RLinf 或 pyAgxArm 源码。现有 RLinf 已提供每个 CAN 通道的跨进程 `DeviceClaim` 独占锁，因此未引入带任务接管逻辑的 `hardware/lease.py`。

## 本机使用现有依赖

此提取版是控制库，不是完整实验程序。复用现有环境和 SDK，不安装、升级或修改外部依赖。硬件 Python：`/home/youlika/miniconda3/envs/RLinf/bin/python`；RLinf 默认位于 `/home/youlika/code/github/RLinf`，可通过 `PIPER_RLINF_ROOT` 指定。原包初始化逻辑自动加入该路径。

在当前项目根目录，以如下环境加载本地代码和外部 SDK：

```bash
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/src:$PWD/.runtime/deps/api:/home/youlika/code/piper_master_slave_data_collection/.runtime/deps/pyagx"
export PIPER_CONFIG_PATH=/home/youlika/code/piper_master_slave_data_collection/.runtime/config/piper_config.ini
```

`PYTHONPATH` 应优先指向本项目 `src`，不要同时加入原项目 `src`，避免同名包混用。随后用上述硬件 Python 导入：

```python
from robot_workbench.hardware.piper import PiperArmController
```

这里只给出导入方式，不执行连接或运动。后续执行具体脚本前按任务设置这些环境变量。原配置只读复用，不调用 `save_config()` 写回原项目。不设置 `PIPER_CONFIG_PATH` 时，默认读取本项目尚未建立的 `.runtime/config/piper_config.ini`，默认构造会报配置缺失；也可通过现有 `firmware=` 参数显式选择经核实的档位。

原安装脚本声明的 pyAgxArm revision 为 `4b0f06585db3324222616999afeda9037df2e8bd`，并指定 python-can `4.6.1`、wrapt `1.17.3`。这是安装声明，未通过 Git 核实实际外部依赖 revision。换机器时需另行准备这些依赖与 RLinf 环境。

## 后续模型接入边界

- 读取关节：`read_joints().radians`，六轴弧度；夹爪：`read_gripper().width_m`、`.force_n`，米和牛顿。反馈具有完整性和时效检查。
- 下发统一使用 `command(joints_rad=..., gripper_width_m=..., force_n=...)`，读取返回的实际限幅目标。`enable()` 是显式使能操作，必须在已获授权的实验流程内调用。
- 继承的 `move_joints()`、`move_gripper()` 等接口仍存在；不要让模型直接调用它们绕过控制类的校验。当前只是保留已有库，不提供模型调用的隔离或权限层。
- RLinf 设备锁在连接时获取，独占同一 CAN 通道；本次没有申请真实设备控制权。
- `hold()` 只发送一次实测保持目标；`disconnect()` 不失能，也不提供持续保持循环。本次没有复制完整的控制循环和退出保持流程，不能将这组文件直接视为可自主运行的模型控制程序。
- 后续模型接入仍需确定任务、动作接口、控制频率、速度、跟踪误差处理、标定和持续保持退出流程。夹爪默认最大宽度为 `0.1 m`，须按实机核实，不能直接视为当前设备标定值。

## 已完成的验证

- 原始提取的 10 个文件均与来源字节一致，原始 SHA-256 保留。补充注释后，全部文件去除文档字符串的 AST 与原始版本一致，语法检查通过；当前版本 SHA-256 另行记录。
- 使用本项目模块、已有 RLinf 环境和原项目 `tests/pyagx_fakes.py`，将 SDK 工厂替换为假传输。验证真实配置可读取、连接不使能或运动、未使能拒绝命令、原生限幅和 SI 单位、关节缺帧与过期反馈拒绝、故障和失能拒绝、断开不调用 reset/disable，全部通过。
- 未连接真实 CAN，未验证机械臂动作、真实反馈时效或实体急停效果。相机和模型接口的本轮验证见下文。


## 相机观测

`src/robot_workbench/hardware/cameras/` 从原项目提取 11 个文件，保留线程采流、帧结构、双相机管理、RealSense/Dabai 后端和两个读取现场配置的独立 CLI。旧 Dabai 后端是现有相机工厂的直接依赖，因此保留；没有复制网页预览或写死设备序列号的额外入口。

代码只补充中文注释，去除文档字符串后的 AST 与来源一致。相机帧是 **BGR**；作为本地 JPEG/PNG 文件传给 OpenAI 时，由图像编码器正确编码，不能直接将 BGR 数组当 RGB 请求。

```python
from robot_workbench.core.config import load_config
from robot_workbench.hardware.cameras import DualCameraRig

config = load_config()
cameras = DualCameraRig(
    config.third_person_serial, config.wrist_serial,
    width=config.camera_width, height=config.camera_height,
    fps=config.camera_fps, warmup_frames=config.camera_warmup_frames,
    backend=config.camera_backend,
)
```

上例仅创建对象；实际采流使用 `start()`，`snapshots()` 返回按 `third_person`/`wrist` 区分的最新帧副本，必须在 `finally` 中 `close()`。沿用现场设备占用检查，不能抢占已有相机会话。

单相机检查可直接使用已有 CLI（按前文配置环境）：

```bash
/home/youlika/miniconda3/envs/RLinf/bin/python -B -m robot_workbench.hardware.cameras.devices.configured_third_person info
/home/youlika/miniconda3/envs/RLinf/bin/python -B -m robot_workbench.hardware.cameras.devices.configured_wrist photo --output /tmp/piper-wrist.jpg
```

## OpenAI 图片与文本接口

接口来源为用户指定的 `https://github.com/selfevolv/creator`，远端版本 `80706ac424c77a42255dba0cf4f89d782e36ae16`，通过 GitHub API 获取；没有执行 Git 命令。本机 Creator 副本较旧，本次使用远端最新代码，不使用旧的纯文本接口。

`src/robot_workbench/integrations/creator/` 中保留三个原模块 `api_backend.py`、`model_catalog.py`、`image_batch_types.py`。前者提供图片输入及结构化返回，后两者为端点/代理校验、模型列表和容量错误等直接依赖。`engine.py` 仅逐函数和逐常量提取配置读取与 JSON 解析所需部分，不加载整个媒体编辑应用。原接口内的检索、图片输出分支随模块保留，本项目未使用这些分支。

所有来源函数的执行逻辑保持不变，仅补充中文注释。未复制 Creator 配置、API Key、模型或媒体编辑代码；OpenPI 专用代码已撤下。

硬件环境原先缺少 `httpx`。本轮按 Creator 的 `httpx==0.28.1` 安装到本项目 `.runtime/deps/api`，不修改共享 Python 环境；`Pillow`、NumPy、OpenCV 复用现有环境。依赖声明为 `requirements/model-api.txt`。其他机器可用相同硬件 Python 执行：

```bash
/home/youlika/miniconda3/envs/RLinf/bin/python -m pip install --target .runtime/deps/api --no-compile -r requirements/model-api.txt
```

通过原接口显式传入配置，不隐式读取其他项目的凭据：

```python
import os
from robot_workbench.integrations.creator.api_backend import run

result = run(
    '请结合第三视角和腕部图像描述目标与夹爪关系，返回 JSON。本次只观察。',
    images=['/tmp/piper-third.jpg', '/tmp/piper-wrist.jpg'],
    system_prompt='你是具身智能实验的视觉观察助手。',
    config={
        'base_url': os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'),
        'api_key': os.environ['OPENAI_API_KEY'],
        'model': os.environ['OPENAI_MODEL'],
        'timeout': 30,
        'proxy': '',
    },
)
```

`run()` 在不启用检索或图片生成时调用 `/chat/completions`，把本地图片编码成 `image_url`，返回解析后的 JSON 对象。`images` 中的两条路径须是实际采集并编码保存的图片；示例不自动拍照，也不驱动机械臂。状态和任务可写入 prompt，当前尚未定义本项目模型输出的机械臂动作协议，不可将任意模型 JSON 直接执行。

## 本轮功能检查

- 两台真实 D435i 均完成 640×480、30 FPS 配置下的连续取帧和双相机同时采流检查；同时采流时两路均在 0.5 秒内增加 15 帧。本轮一次最新帧时间差为 8.63 ms（不是硬件同步保证），帧序号和单调时间戳递增；已查看第三视角和腕部照片，画面可用。本次使用现场的 120 帧预热值，检查后释放相机；未中断已有占用进程。证据保存于 `.runtime/verification/`，摘要写入来源清单。
- `tests/test_camera_model.py` 的 7 项离线测试通过：包括帧副本和释放、双相机一路失败时清理、双图请求的原始字节、状态文本、JSON 解析、配置缺失和错误响应。HTTP 使用假传输，没有发送真实模型请求。
- 真实 OpenAI 服务尚未验证：本机 Creator 默认位置未找到 API 配置，等待用户提供配置路径或服务参数。图像到模型请求协议已验证，不代表真实模型识别效果或机械臂视觉闭环已验证。

复现离线测试：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:.runtime/deps/api:/home/youlika/code/piper_master_slave_data_collection/.runtime/deps/pyagx /home/youlika/miniconda3/envs/RLinf/bin/python -B -m unittest discover -s tests -v
```
