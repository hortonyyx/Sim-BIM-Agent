# Textured mesh 可选只读 worker 接线

2026-10-19。用户在首批实现后要求“继续推”。本轮扩展自主派工，公共 runtime、预算、恢复、源 BIM 和 kernel 继续共用；不改变图纸默认分工，不启动真实模型或更新桌面。

最终开发版本 **domain118 / runtime31**。独立审阅确认两项真实配置/恢复问题及一项重试关联缺口已修复，当前没有剩余具体P1/P2；正式冻结入口含恢复的最后复验通过。

## 实现

- `single_model` mesh 案例可设置 `mesh_workers: {"max_concurrent": 8}`，省略则不开启。沿用本案模型、route、思考/输出和计价参数；同一个调度员既可亲自处理，也可通过 `delegate_mesh_workers` 提出任意有界问题。没有强制立面数量、固定角色阶段或自动新增任务。
- 每项任务显式给模型/工具额度，可给秒数/token子上限。子任务直接使用公共 Runtime 和 EventStore.for_task，全部请求、未知用量、工具尝试及费用共用根账本；子额度不增加整案额度。有限整案时间时沿用现有并发任务的单请求时间预留方式，任务截止与已用额度在恢复时不重置。
- 只复制已准入 GLB、辅助 PNG、文字声明，以及明确选择的观察 JSON/PNG/NPZ 和至多一个源候选快照。禁止硬链接与遍历声明内路径，不复制 GT、其他候选或父目录整包。Worker 在独立目录生成视图、测量与回叠，工具面没有 build/revise/finish/delegate；主 BIM 修改仍属于调度员。
- 结果分为 directly_seen、interpretations、uncertain；允许仅返回不能确定的事项。直接观察必须绑定实际供应或读取的图片，辅助 PNG 的准入本身不算已看；声明与调度员 notes 也不算测量。计算值必须绑定对应 observation 的真实成功 `measure_mesh_pixels` 事件，模型自由文本仍为自报观察。
- `read_mesh_worker` 回读验证后的建议和真实 PNG，图片 ID 带 worker task 前缀，避免不同任务的 mesh_001 混淆。候选回叠标为 source projection，不能冒充原建筑或独立精度；调度员自行判断旧快照意见是否适用于当前候选。新增另一个候选不会使未变化的原 mesh 观察失效。
- 任务身份/参数/配置和证据由事件 blob 绑定；同 ID 完成结果复读不再请求模型。父恢复先核对版本及配置，子恢复复用原 checkpoint，不相信可编辑 receipt.json。恢复重试保留原 retry 事件关联。整案 receipt 单列 `whole_run_counts` 和 `mesh_workers`，真实累计用量继续以公共 `root_usage_accounting` 为准。
- 只读工具现可在选定候选上生成 overlay、转换测量坐标；写 mesh frame 仍仅调度员可用。`read_candidate_items(collection="boundaries")` 分页给实际 source ID、顶点、邻接与开口关系；完整 overlay 投影进入已有带哈希 `tool_reports` 读取接口，未扩大任意路径读取权限。稀疏复制观察后，新 ID 取最大编号加一，避免覆盖输入证据。

## 验证边界

所有检查使用离线脚本响应或本地确定性工具，真实模型调用为 0。组件并发与恢复、真实 stdio MCP、正式冻结入口分别检验，不能从父任务 completed 推断 worker 成功，也不能从 scripted 交付推断自主质量。

首次正式入口检查发现：完整 JSON context 配置的 `pinned_tags=[]` 被按 Python strict tuple 校验，两个 worker 因而拒绝启动；父调度员仍完成预设 BIM，累计只有父 7 请求。测试通过整案计数应为 12 的断言正确报错。修复采用 JSON 配置还原并增加完整 context roundtrip；另补二次中断后的 retry 事件关联。原失败回执保留，不覆盖或计作成功。

第二次冻结入口已完成父7+子4/1共12脚本响应、10工具和有效交付，但同配置恢复检查误拒绝：公共Runtime在run时才加入context_image的read grant，而domain提前恢复守卫先比较了尚未加入该权限的role。修复让父/子domain入口在提前守卫前具备同一权限；Runtime原有幂等接线保持。此轮只算建模脚本链通过，恢复失败单独保留，不计完整通过。

| 检查范围 | 结果 | 耗时 | 证据与边界 |
|---|---|---|---|
| domain116，8模块集成：worker配置/证据/派工、版本、冻结工具、R3公共完成、mesh完成、配置 | 120通过、1失败 | 337.89秒 | 唯一失败为完整context还原问题；`cache/pytest/mesh-workers-integrated/b19607b959e3/results.xml` |
| 真实stdio MCP：readonly回叠、boundary读取/大记录分页、原mesh工具与A3T | 43通过 | 57.98秒 | 三模块；没有真实模型，未评真实建筑 |
| domain117，入口模块再次复验 | 11通过、1失败 | 53.31秒 | 实际派工与交付已成功；失败为随后同配置resume误拒绝；`cache/pytest/mesh-workers-entry-final/72a9c313d623/results.xml` |
| 独立审阅第一轮及第一组修复复核 | 58通过；定向4通过 | 23.29秒；5.42秒 | 与集成检查有重叠，不直接相加；另注入transport_error+UsageMissing证实1请求、3157 token预留不退款/不重放 |
| domain118，完整context权限与子任务中断恢复 | 作者定向4通过；独立专项1通过 | 4.09秒；2.48秒 | 子任务实际读图/2响应后中断，同ID恢复completed；请求数[2,0]，没有工具重读；`cache/mesh-workers-context-resume-20261019.xml` |
| domain118，正式冻结MCP父入口交付与恢复 | 1通过，0失败 | 37.56秒测试墙钟；receipt22.735秒 | 父7+子4/1共12脚本响应、10工具，两个worker均completed；交付有效，同配置resume=already_completed且journal字节不变；变更并发参数恢复被拒绝且journal仍不变 |
| 最终版本及Git暂存字节核验 | 通过 | — | 447个domain文件+46个runtime文件，共493项登记哈希与暂存字节一致；公共Runtime实现无修改 |

上述 `cache/` 均位于 `D:/Sim-BIM-Agent/`。脚本 usage 固定为每响应20，总数仅用于验证记账；不是服务账单或真实模型token消耗。初期两次失败记录完整保留。

最终成功证据：`D:/Sim-BIM-Agent/cache/pytest/mesh-workers-resume-final/1232f0e6a53e/results.xml`；运行位于其`temp/test_native_mesh_workers_retur0/native-workers`，receipt保留全run12请求、模拟240tokens、分task计数及已完成恢复状态。独审未知用量与恢复注入分别保存在`cache/mesh-review-unknown-o_5l6_kd/audit`、`cache/mesh-review-context-resume-go4nxnt_/audit`。此前120个通过检查继续有效，两个入口失败均经后续同一完整链路复验修复；没有遗留已知测试失败，不扩大为历史全套通过。

## 未覆盖与后续

- 本轮没有真实模型自主派工、真实建筑独立精度或速度实测。5–10分钟/常规15分钟仍是目标；外体量、外窗与合理未知内部的质量口径保持。
- 真曲线、带孔空间、斜墙面、沿高度变截面、通用原 mesh/source 对应与自动精度检查尚未实施；新增几何合同仍未接 EnergyPlus。
- 输入准备开始后、父 journal admission 写入前若进程退出，不自动恢复同 ID：可能留下不完整工作区，也可能已有 packet 但其原 timing 尚未绑定父记录；相同任务 ID 明确拒绝，可由调度员换新 ID。该阶段尚无模型请求，不重置已产生的调用或费用；本轮不增加自动清理未知目录的恢复机制。
- 下一步需选定贴图完整的单栋案例，固定辅助信息、角色/模型、预算和验收范围，再按当轮真实测试授权运行。当前只有候选文件元数据核对，尚未认定为完整贴图基准。
- 桌面固定目录仍为 runtime24/domain110，本轮未发布。旧实验、旧案例和其他工作树保持；没有新建工作树需要清理。
- 收尾磁盘快照：C盘余35.09 GiB，D盘余120.73 GiB。正常提交和推送主分支；中间domain116/117登记作为不可变历史保留。
