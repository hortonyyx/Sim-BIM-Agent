# Mesh 首稿修复与固定 ROI 分工迭代

本轮承接用户“继续迭代、分工方式继续探索”的授权。目标是减少首稿前的串行纠错和小子任务的取图往返；交付档位与停止条件仍留待讨论。代码 `149bd43f6929065d04dbf379b51e8c21b3e1fed7` 已推送，runtime42/domain141。domain140 是运行前的一次中间登记，追加来源冲突防护后登记141，旧记录未改。没有修改旧案例结果、放宽几何检查或更新桌面副本。

## 实现范围

1. **独立字段问题一起反馈。** 参数计划中已使用模板的用途证据、门窗 kind/state 和核心连接字段复用正式 schema/编译器规则；即使几何规整先失败，也能附带精确 JSON pointer 与允许值。附带诊断不是新准入门，正式 prepare/expand/build 仍决定能否保存，不自动填值。独审发现并修复了全 exclude、由显式实例替换的 repeat 不应校验闲置模板的问题。
2. **失败补丁保留完整意图。** 仅在尚未生成新草稿的操作/指针失败时保存 failed_batch，并绑定 run、原 base、源文件及完整批次 hash。模型可显式 replace/remove/insert 原批次中的单项，未改项保持，修正后整批原子校验。写入前 exclusive claim 防重复；完整结果可幂等读回，中断则给已保存草稿/候选的恢复依据，不自动重复建模。重试模式与新 overlay/inference 副作用分开调用。
3. **辅助裁图首轮供应。** delegate_mesh_workers 可选 image_view_ids，最多 3 张父任务已正式 view_image 的裁图。核实际成功工具回执、原图/视图/返回 PNG hash，复制冻结后在 worker 首请求发送原裁图。直接观察仍引用原 PNG 文件名与原图像素范围；其他仅复制但未供图的图片不能冒充已看见。原图与发送裁图分别入原生账务，恢复和正式 read_mesh_worker 读取保持来源绑定。

生产 worker 仍等待整批结束后返回，模型路由仍继承主控。非阻塞分工仅完成生命周期设计与 17 个反例清单：入队前冻结、崩溃恢复、主控退出前结算及共同预算都需要一起实现；本轮没有把约 145 秒历史等待宣称为已消除。

## 离线证据

只读重放旧利园、新利园与幸福中心首稿，分别可同时附带 1 项 role、role/kind 各 1 项、2 项 assumptions 与 6 项 kind 缺失；原几何错误仍保留，原 run hash 未变。它证明同轮可提供的信息增加，不证明模型一定少用相应轮数。

相邻 7 模块初轮 203 项通过、2 项失败，189.39 秒。两项为 plan assembly 与 drawing_process 旧失败，在干净 d6fa0bfe 导出树中同样复现（9.68 秒），不计入本轮通过。ROI 来源修复后，恢复/回执/新测试 39 项通过（24.69 秒）；独审 native wire、durable response 恢复和 imported reader 反例另 8 项通过（5.14 秒）。范围有交叉，不把这些次数相加充当独立覆盖数。

独审捕获了首请求把裁图 SHA 误作原图 SHA 的真实缺陷。均匀红图夹具会让原图和放大裁图恰好同 hash，现以非均匀原图复验；原失败记录保留。新测试模块均登记 targeted，普通检查使用 no-billed gate，未调用提供商。

最终独审：字段诊断及相邻编译器63通过/3.37秒；patch29项、编辑路由17项及独立反例5项，共51通过/15.60秒；ROI含不同原图同裁图字节、mesh与裁图同字节冲突共10通过/4.90秒。来源歧义时保守拒绝，并提示分任务。两个来源冲突反例已移入正式测试，7项复验通过/3.61秒。成功重试 wrapper 同时带 overlay/inference 的副作用已阻止；附带诊断三类异常不替代正式 parser 决定。

## 真实验证范围

授权范围内安排一项 Sol6.1/high 合成补丁协议验收，以及固定同 ROI 的 Qwen27B thinking on/off、Sol 首轮供图、Sol 按需读图四个对照。各条件单样本；输入、输出预算、真实与脚本主控调用、准备与执行耗时分别记录。小验收不替代整案质量和端到端速度测试。

### Sol 补丁协议验收

原生 `gpt-6.1-sol/high`，订阅路线、32k 本地输出预留，8模型/14 runtime工具/300秒上限；前置确定性正式 MCP 建立合法合成单房间与43操作失败，另计2工具，全程上限16。输入明确是协议夹具，不是旧建筑结果或质量参考答案。启动前 available commit 29.82GB，高于8GiB保护。

实际52.113秒、5模型/5 runtime工具（含准备共7工具），输入50,119/output630/合50,749 reported tokens，completed。模型只改原批次第43项，42项原值逐项保持，正式 inspect 读回 assumptions/connections，再以完全相同重试参数获得 reused 回执。恰新增一个候选、一个草稿和一个patch，几何与其它声明保持。21项只读协议审计通过；实际 native 请求均Sol/high，原输入与代码/脚本/seal运行前后不变。

独立审阅另做16项交叉检查，全通过：5个模型请求/响应与实际工具调用一致，工具的 shown 回执确实进入后续原生请求，整个原run文件hash前后不变。相同合成批次的 operations_json 由7,298字符变成509字符（减少93.03%）；这是表达长度证据，不是整案 token/时间收益。

### ROI 控制器修订记录

首次启动在 scripted parent 的第一响应因缺少 usage 进入 `token_usage_unavailable`，没有执行父工具、没有派发worker、0真实模型调用，5.220秒退出。原始probes脚本、seal和失败run保留。修订仅在外部新目录probes-r2声明脚本响应的synthetic zero usage，并先以假adapter+正式MCP离线贯通；该零值不算提供商用量。产品冻结版本、题目和真实模型配置保持。

r2正式运行前，完整正式MCP离线smoke为1项通过/14.30秒、9个检查全真；真实提供商0调用。r2随后串行执行原定4腿，全都completed。以下耗时包含父取图、输入复制、MCP准备、worker和回读退出；真实请求/tokens仅计worker，脚本父主控不是真实Sol调用。

| 条件（各一次） | 端到端 / worker秒 | 真实请求 / worker工具 | reported tokens | 行为与观察 |
|---|---:|---:|---:|---|
| Qwen3.8-27B thinking on，预取 | 165.78 / 157.48 | 1 / 0 | 16,749 | 直接使用裁图；5种文字及楼梯符号可见。输出6,467 tokens，其中5,920为provider报告的reasoning tokens。 |
| Qwen3.8-27B thinking off，预取 | 39.24 / 31.69 | 3 / 2 | 31,347 | 又读一次带网格原ROI，再放大2.5x；不是格式修复。文字/符号基本可用，但4条观察都输出了题面禁止的ROI坐标，不能算完整任务通过。 |
| Sol6.1/high，预取 | 30.70 / 18.87 | 1 / 0 | 8,082 | 直接给出5种正确文字，保持坐标空、无越界推断。 |
| Sol6.1/high，按需读图 | 36.30 / 27.09 | 2 / 1 | 13,850 | 自行调用同ROI的2x无网格图后回答；5种文字和第6项便器符号均有直接图证。 |

四腿共7次真实worker请求、70,028 reported tokens，批次274.37秒；共享盲seed准备0.037秒另列。全部首请求供图、原图/发送图分离、实际wire thinking/effort参数审计通过；结束再次核验冻结源码、registry、脚本、seal与原始输入不变。调用使用既有Paratera Qwen3.8-27B API和Sol订阅，**不是本地27B速度测试**。费用未取提供商账单，不能从跨模型tokens直接判断更便宜。

Sol本样本少一次请求，端到端少5.59秒、reported tokens少41.65%。但预取是650×380、按需是模型选择的1300×760，且每条件仅一次、服务与启动耗时不同；不是严格同字节随机对照，不推算整案节省比例。Qwen开思考展示了单请求仍可耗时很长，不能只靠减少工具数判定提速。Qwen关思考展示了“已经供图”提示并未阻止额外读图，通用schema也未拦住本题禁用字段。

## 分工下一步

当前证据继续支持固定ROI文字/有限符号作为最小候选；不支持让弱模型自行决定楼层对应、几何深度、同墙身份或拓扑。下一步应先测试**已有裁图、一次回答、无额外工具、有限输出字段**的子任务合同；用已有每任务预算可先做只读探针，任务专属字段限制仍需单独设计。不能把全局worker schema合法当作每题遵从，不能靠无限复读弥补边界。

主控Sol继续决定观察位置、需要什么信息以及是否采用；程序负责裁图/hash、坐标范围、字段/引用合法性与确定性差分；子任务只提交少量识读结果及unknown。进入几何/拓扑的内容仍由主控判断，不自动采用高置信文字。应继续记录核验新增调用和关键路径，而不是只比较子模型单价。本批次不挑选重跑失败或不理想样本，没有启动更多模型。

## 外部证据入口

- 实现及离线记录：`D:/Sim-BIM-Agent/cache/mesh-harness-iteration-20261023b/`，含 preflight、patch、review、ROI 与干净基线复验。
- 非阻塞设计：`D:/Sim-BIM-Agent/cases/mesh-harness-reliability-20261023b/design/`。
- ROI 对照：`D:/Sim-BIM-Agent/cases/mesh-harness-reliability-20261023b/probes/`。
- 修正控制器后的 ROI 对照：同案例目录下 `probes-r2/`；原失败输出保留，不覆盖重跑。
- 补丁协议验收：`D:/Sim-BIM-Agent/cases/mesh-harness-iteration-20261023b/acceptance/patch/`。

代码独审入口是cache下 `review/INDEPENDENT-REVIEW.md` 与 `review-snapshot.json`；实际patch独审为 `PATCH-ACCEPTANCE-REVIEW.md` 与 `patch-acceptance-independent.json`。ROI盲参考与视觉独审为 `ROI-VISUAL-REFERENCE.md`、`ROI-VISUAL-REVIEW.md` 和 `roi-terminal-independent.json`；四个原run的288/404/329/342文件哈希分别核验不变。ROI原生审计与参数/图像来源断言在 `probes-r2/evaluation/`，总调用/耗时及运行后seal复核在 `probes-r2/RESULTS.json`。测试、失败尝试与原run均保留，不以当前读后分析修改其journal。

旧版两案速度与质量边界继续以 [上一轮报告](2026-10-23_mesh_harness_reliability_and_delegation.md) 为准；本轮未重新计时利园整楼，不能把局部节省折算为新的总用时。

收尾时本轮5项真实任务全部终态，共12次真实模型请求；没有后台模型任务，未新增Git worktree或发布桌面。2026-10-24 00:38本机C盘约37.47GiB、D盘约103.77GiB空闲；既有`.playwright-cli/`未动。外部大证据仍在D盘，不混入产品或记忆。

## 后续夜间授权

00:40前后用户追加持续推进授权：优先档位设计之外的harness迭代，新增真实测试统一GPT-6.1 Sol，按需自由安排；分工仍只探索，早上汇报后再讨论。上述Qwen结果属于这条新约束之前的已完成批次，不继续新增Qwen调用。

已设置本线程夜间整点续跑及10月24日09:00（Asia/Singapore）汇报，automation id `mesh-harness`。下一阶段外部证据根为 `D:/Sim-BIM-Agent/cases/mesh-harness-overnight-20261024/`；先准备当前版本利园原输入冷启动验收、只读分析下一项非档位开销、设计闭环立面子任务。实际启动以该目录status/seal和原生run为准，不把计划写成完成。
