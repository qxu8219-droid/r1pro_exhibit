# 展会主屏的只读实时数据

默认启动只读取已有回放，`实时观察` 显示未接入。服务器不会连接机器人、发布动作、调用模型、读取未配置的路径或把四机模拟转换成真机数据。

第一版可接一个本机 JSON 快照文件，由现场适配器提供真实相机与任务阶段。展台机器仍只需 Python 3.9+ 标准库和本地浏览器。

## 启动

在项目根目录运行（路径替换成现场适配器实际写入的文件）：

```bash
python3 exhibit/server.py --port 8771 --telemetry-file /absolute/path/robot_snapshot.json
```

Windows 同样支持绝对路径，含空格时加双引号。浏览器选择“展会主屏 → 实时观察”。也可直接使用启动器：`run.bat --telemetry-file "绝对路径"` 或 `sh run.sh --telemetry-file "/绝对路径"`。启动器仅传入只读文件配置，不生成遥测文件。

如果先单独启动服务，随后启动器必须传相同的实时文件配置才能复用；否则提示端口冲突。相对数据库或快照路径以 `exhibit/` 为基准，建议用绝对路径。

## 数据合同

```json
{
  "schema_version": 1,
  "robot_id": "实际机器人身份",
  "run_id": "实际运行身份",
  "observed_at": 0,
  "task": "实际任务名称",
  "model_id": "实际模型版本",
  "stage": "现场执行器回传的当前阶段",
  "images": {},
  "request_id": "实际请求身份",
  "rtt_ms": 120
}
```

这是结构示意，`observed_at: 0` 会被拒绝。必填字段为 `schema_version / robot_id / run_id / observed_at / task / model_id / stage`。`images` 可省略，`request_id / rtt_ms` 可选；不知道的信息不要伪造。没有模型时 `model_id` 可明确写“未使用动作模型”。

- `observed_at` 为 **这组观测实际采集时刻** 的 UNIX 秒，支持小数。不能给旧图片填新的写入时间；多相机组合使用最早采集时刻。生产者和展台时钟需要对齐。
- `images` 可包含 `head / wrist_left / wrist_right`。值为 `data:image/jpeg;base64,...` 或 `data:image/png;base64,...`，不能填网络 URL 或本机图片路径。单图最多约 2 MiB 字符，整个快照最多 8 MiB。
- 每份快照只能来自同一台机器人、同一次运行。写入方先生成完整临时文件，再原子替换目标文件，避免读取半份 JSON。展台每秒只读一次。
- 采集时间在未来或超过 5 秒，API 返回 `connected:false / status:stale`。前端清除图片与执行计数，不用回放填补实时画面。缺失、损坏或读取失败同样显示未连接。
- 当前实时合同只包含相机、身份、任务阶段与可选请求时延，不包含完整动作/反馈序列和任务成功验证。界面缺失项显示“—”；连接正常、阶段文字和夹爪指令均不作为完成证据。

## HTTP 接口

`GET /api/telemetry` 返回：

```json
{
  "source": "live",
  "configured": false,
  "connected": false,
  "status": "unconfigured",
  "age_s": null,
  "state": null
}
```

状态包括 `unconfigured / connected / stale / missing / invalid / too_large`。过期状态可能保留白名单清洗后的 `state` 用于诊断，**只有 `connected:true` 才能展示为新鲜观测**。不向浏览器返回配置文件路径、额外字段或凭据。

`GET /api/evidence?file=<static/replays 中的文件名>` 返回 `{source:"recording",file,data}`，从已有回放 HTML 提取 JSON，不执行脚本。主屏回放与模型详图使用同一份记录；时间跳转不会借用另一个运行的图片或未来事件。

## 展前真实接入仍需完成

在选定机器人上核对现有相机、执行器和日志版本，再实现输出上述快照的只读适配器。快照生成与传输尚未连接任何真机；本地协议及合成快照测试不能代替真机验收。涉及医院交付时，另接动作后观测或现场确认作为结果证据。
