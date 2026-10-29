# 新IP现场验证与显示时钟对齐

用户更新机器人为 nvidia@10.209.190.56，并明确授权启动原 start.sh --enable-motion 做现场运动测试。原脚本与配置不得改动；新增输出限定在 /home/nvidia/r1_exhibit_observer。此前默认隧道配置公钥认证失败，随后用户自行接通 ws://127.0.0.1:18128 并通知。收到现场运动授权后，执行了一次原启动命令，没有更改原脚本与配置。

## 已完成

- SSH、独立采集器版本和存档恢复检查；机器人原启动脚本、配置、executor/backend、客户端及source manifest哈希与10月27日基线一致。
- 启动已有相机程序，输出、ROS日志和GStreamer缓存全部重定向到新增目录的camera子目录；不编辑原相机程序，不更改VLA输入规范。
- 三路真实JPEG均为960×768，并在浏览器实际解码显示。
- 只读测量发现机器人时钟快约13.52秒；机器人NTP未完成同步，本机已同步。未修改系统时钟。
- 实现显式 --robot-clock-offset（默认0，范围±300秒），只校正接收及API数据年龄，保持原始字节、源时间戳、RTT与回放不变；非零值进入服务身份并由API诊断字段返回。
- 新IP写入展示启动器默认值及当前操作说明，历史设计文档和原回放不改。
- 新鲜度、原字节保持、错偏差服务复用、无效偏差及完整启动/持续接收均经先红后绿回归；展示端145项测试和两个浏览器回归通过。

## 本次验证数据范围

初始新验证库触发大体积历史回补，导致相机下载延迟。旧记录完整保留；本次另启独立采集区 /home/nvidia/r1_exhibit_observer/verify-20261029/runtime，只从当前会话开始观察。本机当前使用 /home/xq/r1pro_live/verify-20261029-live。此前 /home/xq/r1pro_live/verify-20261029 保存时钟测量与历史回补诊断，不作为本次运动实时证据。

```bash
./start_live.sh --robot-ip 10.209.190.56 --robot-clock-offset 13.6 \
  --no-ssh --no-browser --port 8773 \
  --snapshot-file /home/xq/r1pro_live/verify-20261029-live/snapshot.json \
  --db /home/xq/r1pro_live/verify-20261029-live/fleet.sqlite3
```

13.6是本次测量采用的保守值，不是机器人永久配置。时钟同步或重启后必须重新测量，时钟一致时用0。

## 实际运动验证结果

- 本次 session 为 client239_69e729ad76ae41eaa3e37c2b5bc78502；主启动目录与用户提供命令一致。
- 0004_run 收到82次响应、下发682条动作、采集5003条独立反馈；最后因原 joint_posture_guard 保护退出，stopped/ready_for_home=true，不宣称任务成功。
- 原协调器自行 HOME、PRECHECK 后进入0007_run，展示自动跟随新操作及request0；第二轮收到5次响应、下发40条动作后由本次测试正常取消。
- 原停止路径返回 STOP_CONFIRMED/stopped=true；退出后复查本次控制进程均已退出。未增加外部重启或绕过保护。
- request18的state23、32×23动作矩阵、实际下发值、独立反馈、CoT、RTT和三张224×224输入的逐像素哈希均与原JSONL/NPZ一致。不是嵌入回放数据。
- 两轮request_failures=0，原事件中没有reconnected/reconnect_failed。首轮平均RTT1.979秒、最大7.982秒，服务端计算平均0.429秒；时延存在波动，不能断言网络完全稳定。
- 2分钟24个在运API样本中VLA证据年龄均值3.261秒、最大11.401秒；相机4个样本超过5秒阈值后恢复，最大10.046秒。仅为采样窗口，不据此声称整个过程低延迟。
- 只读验证与明细保存在 /home/xq/r1pro_live/verify-20261029-live/，包括source-exact-match.json、api-monitor.json、api-monitor-late.json及previous-run-timing-report.html。
- 用户随后要求关闭展示台，已正常停止本次本机接收器和8773服务；机器人侧相机与只读采集器仍保留。后续开发和时差检查没有重新启动运动。

## 一键入口补充

用户希望只运行start_live.sh即可就绪，新增live_settings.json保存本次参数，自动只读测量时差，见2026-10-29-one-click-live.md。真实只读SSH探测成功（当时13.445796秒，RTT12.5毫秒）；使用该值的--check确认三路画面有效。没有再次启动展示服务。
