# 接入真实模型

默认的 `baseline` 是确定性的规则基线：选择当前可执行候选中优先级最高的一项，同优先级按 ID 排序。它不调用模型，不代表 VLM 或 Agent 的模型能力。

`model` 模式会通过 Python 标准库 `urllib` 向明确配置的完整 Chat Completions 兼容端点发起真实 HTTP POST。当前原型向模型发送**合成的世界状态和候选技能 JSON**，没有相机输入，没有验证 VLM 图像识别，也没有连接真实机器人。

## 配置

在启动服务的终端中设置环境变量。服务端配置在构造 Planner 时读取；修改配置后重启服务。浏览器不能写入模型 URL 或密钥。

| 环境变量 | 含义 |
| --- | --- |
| `FLEET_MODEL_URL` | 完整 Chat Completions 端点，包括路径，例如本地服务的 `http://127.0.0.1:8000/v1/chat/completions`；路径由你的服务决定 |
| `FLEET_MODEL_NAME` | 服务支持的实际模型名称 |
| `FLEET_MODEL_API_KEY` | Bearer 密钥；回环地址可省略，远程端点必须显式设置 |
| `FLEET_MODEL_TIMEOUT` | 单次网络操作超时，单位秒，默认 `20`，范围 `0.1`–`120` |
| `FLEET_MODEL_JSON_FORMAT` | 默认 `1`，发送 `response_format: {"type":"json_object"}`；不支持该字段的兼容服务可设为 `0` |

示例只展示本地、无需鉴权的端点配置。请替换成你已经运行的服务和模型；这些值不会启动或下载模型。

```sh
export FLEET_MODEL_URL='http://127.0.0.1:8000/v1/chat/completions'
export FLEET_MODEL_NAME='your-served-model-name'
export FLEET_MODEL_TIMEOUT='20'
export FLEET_MODEL_JSON_FORMAT='1'
```

随后按 README 启动原型，在界面切换到 `model` 模式再开始。远程模型需要 HTTPS，并通过启动环境提供密钥。请不要把密钥写进源码、URL、截图或提交到版本库。端点不接受用户名、密码、查询参数或 URL 片段；不会跟随重定向，也不会继承环境代理。

配置状态仅显示模型名、端点的 origin、是否提供密钥、超时、格式开关和错误；不显示密钥或端点路径。配置有效仅说明静态检查通过，**不表示模型服务已连通或模型名称存在**。

## 调用协议

请求包含 `model`、`messages`、`max_tokens: 256`、`stream: false`，以及可关闭的 `response_format`。这是本原型明确支持的兼容协议；如果服务使用其他字段，例如只接受 `max_completion_tokens`，需要修改适配器后验证。

模型必须以单条 `choices[0].message.content` 文本返回严格 JSON，且只能包含以下两个字段：

```json
{"candidate_id": "提供的某个精确候选 ID", "reason": "根据当前观测给出一句简短决策说明。"}
```

确有等待理由时可返回 `"candidate_id": null`。不接受 Markdown 代码块、额外动作参数、未知候选 ID、重复 JSON 字段或超长解释。`reason` 是可展示的决策摘要，不要求逐步思维过程。

Planner 只选择候选，不直接派发任务。服务须将返回 ID 映射到该次输入的原始候选，再交给执行器验证 `epoch`、世界版本、资源占用和任务条件。模型推理期间暂停、重置或状态变化，会使旧提案失效。

错误、超时或不合法输出均返回 `status: error` 和 `candidate_id: null`，界面显示错误，**不会偷偷切换为规则基线**。没有可执行候选时不调用模型，返回明确的等待摘要。

只发送当前合成世界中的版本、epoch、时间、任务、机器人、车辆、站点和技能候选；不发送历史事件、过往模型决策、环境变量或密钥。此原型的内容检查和候选约束是模拟器接口机制，不构成真实设备的控制安全认证。

## 验证范围

`python3 -m unittest discover -s tests -p 'test_planner.py' -v` 启动临时回环 HTTP 服务，实际检查请求体、Bearer 头、成功响应、显式等待、非法 JSON、虚构 ID、重复字段、额外参数、HTTP 错误、重定向拒绝、超时、密钥脱敏和无模型默认行为。测试不访问外网、不使用真实密钥、不证明某个外部模型的调度质量。

下一步接入 VLM 时，应增加经过授权的图像采集与观察接口，将带时间戳、车辆身份和不确定性的观察写入 WorldState，再复用当前规划与执行链。应先用现场录制样本验证观察质量，不能将本原型中的合成观测当作已完成的视觉能力。
