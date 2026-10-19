# 利園六期第二轮：运行失败与独立核查

用户批准以相同 GPT-6.1 Sol/high、原 mesh、23/F PNG、中文说明及主要预算做一次冷启动完整对照。本轮实际启动后因本机内存分配失败提前终止，**没有 BIM 候选或交付，无法评价第二轮质量和提速收益**。独立核查同时定位了一个与内存失败无关的 worker 预算状态序列化缺陷。

运行冻结代码 `d030243a63788c86aff7bdc4d1a953ecf9777926`，**domain-v123-20261019 / runtime-v31-20261019**。运行中没有补提示、修改候选、调整代码/配置或模型回退；本轮只追加核查记录和项目文档，未修产品代码、未发布桌面。

## 配置与终态

与首轮配置仅改变 `batch_id` 和输出目录：ChatGPT subscription、`gpt-6.1-sol/high`、reasoning history all、每响应本地输出预留32000 tokens、全案48请求/240工具、可选同模型worker最多8个、无整案seconds/tokens硬截止、max_candidates64、image_history_batches3。模型看不到首轮答案、用户修正版或独立GT。

| 项目 | 首轮完整运行 | 第二轮实际模型运行 |
|---|---:|---:|
| domain / runtime | 121 / 31 | 123 / 31 |
| 命令总耗时 | 1767.218秒 | 153.375秒，提前失败 |
| Runtime耗时 | 1723.628秒 | 根receipt缺失，不冒填同口径值 |
| 请求 / 响应 | 27 / 27 | 8 / 8 |
| 主模型 / worker请求 | 27 / 0 | 7 / 1 |
| 实报tokens（输入＋输出） | 4303177 | 303126（300116＋3010） |
| 工具调用 | 93 | 31（30成功、1失败） |
| 参数建模 / 补丁 / finish调用 | 首轮3次完整构建，正式交付 | 全部0 |
| 有效候选 / 正式交付 | 3 / candidate_04 | 0 / 未生成 |
| 源几何、平面、回叠、BIM查看页 | 已生成并独评 | 未生成 |
| 独立≤5/10/30cm累计比例 | 61.71% / 76.41% / 92.08% | 未评，无候选 |

第一列质量是原自主首轮，非随后0模型修订版。两次阶段不同，不计算耗时降低百分比；第二轮更早停止不代表提速。

8次响应均完整且有实报用量，1596 reasoning tokens已含于3010输出，缓存输入0，订阅费用金额未知。主模型297754 tokens、worker5372 tokens。已记录主模型请求104.735秒、worker请求4.813秒；工具含子调用21.158秒，不能相加成互斥耗时。首末事件跨度136.304秒与命令153.375秒分别保留，不伪造缺失的根receipt或结算状态。

第二轮保留两个目录，只有后一个发生模型调用：

- `D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-second-20261019-001`：首次启动21.164秒，SciPy `_fblas` 加载被Windows报系统资源不足，随后MCP连接关闭；0模型、0工具、无events/候选。直接SciPy import随后成功，未改代码/参数，换独立目录重启。
- `D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-second-20261019-002`：2026-10-19 14:15:59.989179 UTC启动，14:18:33.365833 UTC退出1；153.375秒。无恢复、无第三次启动或额外模型复盘。

## 两个独立问题

**主流程内存失败。** `view_mesh` 的 event-000185 报无法分配输出缓冲；event-000193记录coordinator在 `prepare_request` 阶段触发 `MemoryError`。异常退出收集artifacts时，`src/agent_runtime/loop.py` 的 `_stop` 又在 `path.read_bytes()` 触发 `MemoryError`，因此没有根 `receipt.json`。终态由controller退出码、生命周期事件和console traceback共同确认，不能借用子任务receipt代替。

首次启动时观察到约2.32 GiB可用系统提交空间；运行结束后的14:22:09 UTC快照有约5.35 GiB可用提交空间、6.70 GiB空闲物理内存，188个Python进程合计23.80 GiB私有内存。快照中32个stdio MCP Python进程包含启动器/实际解释器层，不能直接当作32个独立服务。它们的所属活动未完全核清，本轮没有终止其他会话的进程。上述事实支持显著资源压力，但没有连续峰值采样，**尚不能断定全部内存由本次运行、某个泄漏或特定外部进程造成**。

**worker无时限预算序列化缺陷。** `facade_levels` 实际发出1次模型请求和1次工具调用。event-000057显示 `inspect_mesh` 成功、`isError=false`；随后event-000058在工具后处理阶段记录ValueError。子任务没有seconds/deadline，`Runtime._remaining()` 返回 `inf`，`runtime_delegation.update_observer_budget` 把它写入状态的seconds；`store.source` 使用禁止非有限数的JSON序列化，于是失败。两位独立审阅者用小内存纯函数复现相同ValueError。这里不能把生命周期的 `tool:inspect_mesh` 直接解释为网格检查工具失败。

`delegate_mesh_workers` 回执顶层 `status=completed` 仅表示批次结束，内部worker为failed、result=null；worker失败后主模型继续自行观察。预算预检这次允许worker真正启动，但后续接线仍不完整，未取得可用观察结论，也没有并行收益证据。

## 独立评价与后续

独立核验未发现candidate目录、source_model/proposal、delivery/selection或finish调用。仅存的子任务receipt为failed且是child作用域，不能用于宣布整案完成。数值及视觉评价记为 **NOT_EVALUABLE**，未启动无意义的渲染，也没有沿用首轮结果充作本轮通过。

本轮没到参数构建阶段，`repeats`、`core_connections`、`patch_parametric_bim` 和代表/异常层回图是否被模型采用均未验证。原有141个离线有效用例及合成21→31层结果仍只代表各自覆盖范围，不被本轮覆盖，也不能代替真实整案成绩。

下一步先做最小修复和环境恢复，再安排同配置新上下文对照：

1. 无时限worker预算状态使用null，有限值保留原语义；离线验证工具成功后能进入下一请求并正常写receipt，不通过强加时间上限或关闭上下文记录绕过问题。
2. 核清本机MCP进程所属和可回收对象，恢复足够的提交空间；补最小的运行前/峰值资源观测。当前不能将未知进程直接判为孤儿并批量终止。
3. 核查低内存异常退出时的最小终态保存，避免artifact整文件读取再次失败而丢失根receipt；不以大规模重构代替具体故障修复。
4. 以上离线验证完成后，重新批准/执行同输入同模型完整测试。当前记录不包含再次运行授权，也未自动重跑。

## 证据入口

- 首次启动失败：`D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/61sol-second-20261019/startup-failure.json`。
- 正式运行配置：`.../audit/run-config.61sol-second-retry.json`；预检：`.../audit/readiness.61sol-second-retry.json`。
- 本轮审计根：`D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/61sol-second-20261019-startup-retry/`，包含启动授权、配置对照、controller、console、输入前后SHA、资源快照及过程核查。
- 独立评价根：`D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-second-20261019-002-evaluation/`。
- 过程指标：审计根下 `process-comparison/failure-process-summary.json`；独立终态/哈希核验：评价根下 `NOT_EVALUABLE_numeric_review.json`。首轮原run所guard的11文件、首轮评价8文件、原inputs3文件SHA匹配；本轮1236个运行文件在独立核验前后SHA一致。这是本次受检范围，不将首轮11文件复核说成再次全量核查4865文件。
- 独立失败审阅：评价根下 `independent_failure_review.json`；包括实际预算hook小内存复现、无候选评价边界和根交接事实复核，未修改产品/运行文件，0新模型/0MCP。
- 三份模型输入前后SHA一致；首轮原始运行和用户修正版未用于本轮模型上下文。完整请求/响应仅保留在运行审计，不进入Git或产品。

本轮无需代码测试或版本登记；文档按diff及链接核查。开发仍domain123/runtime31，桌面仍runtime24/domain110。收尾磁盘快照：C剩余33.01 GiB，D剩余119.42 GiB。
