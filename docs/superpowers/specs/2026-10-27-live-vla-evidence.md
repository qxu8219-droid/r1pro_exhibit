# 实时观察 VLA 证据接入

用户已批准开发，先保存并推送当前改动（ea55847），再开发本功能。

## 范围

- 保持三路相机的来源、布局和刷新不变。
- 原真机回放、2 界面、回放 HTML、replay_panel 全目录及 recording 渲染函数不改。
- 实时观察四区展示当前任务阶段、实际输入、预测、下发、独立反馈、推理耗时和结果证据。
- 任务结果无独立核验时明确待核验，不能将结束、停止、夹爪或回位当成入筐成功。
- 持续保存新运行数据并支持断线补齐；存档与原回放隔离。
- 不启动或停止机器人运动、模型、现有执行器；用户最终授权仅新建 /home/nvidia/r1_exhibit_observer/，所有新增程序、日志、缓存均在其中。
- 不修改机器人任何原有文件、配置、日志或启动命令；不安装钩子，不创建site.env，不调用原相机或技能启动脚本。

## 方案

保留相机快照，新增独立事件通道。新文件夹内的旁路读取原程序已持续写入的JSONL、请求NPZ和SUMMARY，提供带稳定source_id的字节游标查询（标记legacy，缺少生命周期时不推测）。输入NPZ完整可读时显示归档输入就绪，不声称准确infer开始。展示启动器后台接收事件，SQLite 事务保存事件及游标，服务提供只读状态/历史接口；实时分支读取新接口，录制分支不使用它。

现有控制链配置：robot239_config.json，VLA_PYTHON=/home/nvidia/vla_deploy/venv/bin/python3，B041_POLICY_URL=ws://127.0.0.1:18128。

## 事件合同

JSON event: schema_version=1, event_id(string, producer UUID + seq), robot_id, session_id, run_id, operation_id, kind, observed_at(UNIX seconds), monotonic(optional), request_id(optional), payload(object). run_id优先真实协调器ID；旧日志使用session/operation的明确派生身份，不拼接不可靠的协调器状态。request_id包含operation和request attempt，不能使用成功chunk充当请求ID。

kind: operation_started, observation, request_started, raw_response, request_failed, request_cancelled, target, recovery_feedback, operation_finished, status, grasp_detected, release_detected, clamped, artifact_ready, capture_gap。其他已知诊断事件可留档。

payload约定：
- operation_started: mode(run/home/precheck/stop), task, model_id, legacy(bool)
- observation/request_started: attempt(int), state23(23 finite numbers), capture_at, capture_monotonic, observation_meta, task, archive_queued(optional)。request_started仅表示客户端infer调用开始。
- raw_response: attempt, chunk, request_state23, capture_at, rtt_s, request_timing, observation_meta, response{action23,model_id,task,cot_text,inference_timing_ms,cot_generation,server_queue_and_compute_ms,server_queue_wait_ms,server_compute_ms,checkpoint_sha256,horizon,fps,fm_steps}。
- target: chunk,step,target23,feedback23,publish_started,publish_finished,grippers_published。
- recovery_feedback: feedback23,sample_time,stamps,history_index。
- operation_finished: mode,result_kind,result{finished,stopped,ready_for_home,success,at_observation_pose,reason,home_check_error,...},archive。仅run结束修改run执行终态；home另存结果。
- status: state,reason,task,model_id。legacy状态不可强行匹配到不相关run。
- artifact_ready: attempt,images{head_rgb,left_wrist_rgb,right_wrist_rgb}，实际请求NPZ解码后的PNG/JPEG；pending/缺失明确记录。

机器人GET /events?after=<byte offset>&limit=200 → {source_id,next_cursor,has_more,events,health:{observed_at,dropped,legacy}}。GET /healthz。只读接口无控制能力。

## 展示API

GET /api/vla/state → {configured,connected,status,age_s,state}。state={robot_id,session_id,run_id,operation_id,task,model_id,phase,reason,legacy,request,execution,verification,stats,last_event_at}。
request={id,status,attempt,chunk,capture_at,started_at,response_at,state23,action23,rtt_ms,cot_text,inference_timing_ms,server_queue_and_compute_ms,images}；缺失字段null。execution={issued_steps,chunk,step,target23,feedback23,feedback_at,feedback_kind}。verification={status:'pending',run_finished,stopped,home_success,reason,evidence:[{kind,observed_at,detail}]}。stats={events,requests,responses,failed_requests,dropped}。
GET /api/vla/runs → {runs}；GET /api/vla/events?run_id=...&after=...&limit=... → {events,next_cursor}。
相机和VLA连接状态独立。VLA stale保留已记录证据但标记过期，不冒充正在执行；换run清除旧证据。

## 验收

失败重试不串图片；请求开始/响应/下发/反馈分别出现；无返回不显示旧action；run切换不残留结果；断线补齐无重复；重启后历史保存。回放→实时→回放原行为和受保护文件哈希保持一致。测试使用隔离服务与数据库；真实机器人只做旁路读取验证。

## 最终边界确认

用户在开发中明确禁止修改机器人原有内容，只允许新增独立文件夹。未执行原方案的hook安装；install/telemetry实现已从本次未提交工作移除。继续使用用户原来的 start.sh --enable-motion 命令，采集器自动发现新会话；实时信息来自运行中增量日志，而非结束后整批回放。
