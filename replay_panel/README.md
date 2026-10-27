# 模型推理回放面板（参展用）

把一次真机 run 目录（`events.jsonl` + `request_*_obs.npz` + `SUMMARY.json`）变成**一个自包含的 HTML**。
展台机器（Windows / Linux / Mac 都行）只要双击用浏览器打开，不需要 Python、不需要联网。

## 生成

```bash
python3 replay_panel.py <run_dir> [-o 输出.html] [--title 标题] [--subtitle 副标题]
```

例：`python3 replay_panel.py .../runtime/live_run_20261019T093806212123Z`
→ `replay_live_run_20261019T093806212123Z.html`（2.6 MB，43 段 / 344 步 / 129 张图）。

生成端有 numpy + Pillow 时图像走 JPEG；没有也能跑（纯 Python 编 PNG，文件大几倍）。

## 展台播放

- 打开即自动播放、到尾停 2.5 s 后循环，没人操作也一直在动。
- 讲解时：空格 暂停/播放，← → 前后跳 2 s，点或拖时间轴跳到任意时刻，速度 0.5×–4×。
- 鼠标悬停动作图 / 时延柱 可看具体数值。
- 浏览器按 F11 全屏；按 1920×1080 设计，其他 16:9 也能用。
- 嵌入展示台时，按 1 / 2 / 3 切换车队调度、详细回放和展会主屏；主屏可定位到同一记录的同一时刻。独立打开时不依赖外壳。

## 画面上有什么

| 区域 | 数据来源 |
|---|---|
| 模型看到的画面（三路 224×224）+ 采集延迟 + "推理中"徽标 | `request_XXXX_obs.npz`、`raw_response.observation_meta.timing` |
| 动作：预测（虚线，本段 32 步）/ 已下发目标 / 实际反馈 + 目标/反馈偏差 | `raw_response.response.action23`、`target.target23/feedback23` |
| 每段请求时延（服务器 vs 网络） | `rtt_s`、`server_queue_and_compute_ms` |
| 夹爪开度 + 目标 | `target` 第 14/15 维 |
| 运行记录（中文化的 console + 闭合条件/张开指令/安全裁剪事件） | `console`、`grasp_detected`、`release_detected`、`clamped` |
| 时间轴（每块一次请求，高度 = 往返时延；条件/指令/裁剪标记） | 同上 |

23 维布局按 `client_bundle/full23_adapter.py`：左臂 0–6，右臂 7–13，左右爪 14/15，底盘 16–18，躯干 19–22。

“目标/反馈偏差”是当前段已下发记录的双臂 14 关节 MAE（rad）。同条反馈采集于本步指令下发前，未做时间对齐，不能当作跟踪精度。`grasp_detected` 是右爪反馈达到闭合检测阈值，`release_detected` 是后续张开目标触发日志；两者均不直接证明物体抓稳或到达目标。现有两条记录没有独立任务成功判定，结果保持待核验。RTT 高于本次运行中位数 1.5 倍的段在时间轴上标红。

## 接实时

当前详细面板仍为记录回放。展会主屏已提供本机 JSON 快照的只读实时入口，见 [exhibit/LIVE.md](../exhibit/LIVE.md)：支持真实相机、任务阶段和可选请求时延，过期或损坏时清空画面。机器人侧快照生成与传输尚未接入，详细动作/反馈序列的实时播放也尚未实现。

2026-10-27：夹爪明确标注蓝色为指令前反馈；缺少张开后的画面或反馈时显示说明，回退时间会清除提示。原始数据不变。
