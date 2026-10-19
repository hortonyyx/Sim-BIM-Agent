# 利園六期：无时限 worker、低内存收尾与修后复测

用户在第二轮失败报告后明确要求“先修好再跑”。本轮修复已证实的接线和恢复缺口，保留原模型、输入、质量要求及主要预算；真实复测使用新目录和冷上下文，不恢复失败002，也不提供首轮答案或GT。

开发 **runtime-v34-20261019 / domain-v125-20261019**；桌面仍runtime24/domain110，未发布。登记过程中的runtime32/33及domain124为本轮中间验证版本，历史登记内容未追改。最终登记 `changed_modes=[]`，任务指引/建模策略不变。

## 修复范围

- **Domain预算状态：** `update_observer_budget` 对无seconds上限输出null，有限子期限和继承根期限保持。真实Runtime/EventStore加脚本模型验证工具成功后可以继续第二请求、正常写回执及共享根预算。
- **Runtime文件保存：** 停止时用256 KiB分块读取保存artifacts、计算SHA并流式复核已有blob，保留原文件及内容寻址完整性，避免整文件额外分配；中断清理未完成临时文件。
- **Runtime低内存失败记录：** 正常异常处理再次遇到MemoryError时，尽力写小型 `minimal_memory_failure` receipt，明确failed、completion=null、accounting_complete=false。仅记录未重新核算的counts快照，不声称完成交付、完整记账或完整journal。极端资源耗尽时仍不能保证任何写入成功。
- **Runtime与mesh worker恢复：** completed快速复用须磁盘receipt的全部journal原始字段一致，公共Runtime只返回journal锚定原件，不信任调用方追加的报告；worker自身回执没有追加报告，要求整份一致。失败、缺失、改写或损坏的缓存进入正常恢复。仍检查配置、工具状态、domain completion，恢复不能伪造成功或重发已完成请求。独审发现wrapper绕过公共恢复门禁的P2后补齐，双MemoryError序列现可恢复完整回执且0新增模型请求。

## 环境排查

独立只读进程核验没有找到能确认安全回收的本次运行残留；失败002的controller/entry及其专用工具进程均已退出。现存大MCP进程归属仍在运行的桌面应用，不能仅凭创建较早或空闲判为孤儿。未结束其他会话进程、未改全局环境、数值库线程数或分页设置。

系统可用提交空间自然回升：22:39本地时间快照为13.639 GiB，空闲物理内存约9.31 GiB。33线程与SciPy静态导入链不足以证明内存根因；没有连续失败时峰值数据，不能宣布已定位或修复所有内存压力来源。

修后实测的外部控制器每3秒只读采样系统提交/物理余量及本次PID树，启动前要求至少8 GiB可用提交空间。这是该次实验的环境启动条件，不是模型或runtime新增内存/时间预算；采样不是精确峰值，也不会运行中干预模型。

## 离线验证

| 范围 | 结果 | 边界 |
|---|---|---|
| 文件流捕获、低内存失败/恢复、缺失/篡改/损坏缓存、相邻恢复 | 16 passed，4.55秒 | 最终root实现，0真实模型 |
| worker、证据和observer相邻 | 88 passed，28.89秒 | 首轮修复；随后恢复P2另覆盖并复验 |
| runtime协议、child/budget/control恢复 | 104 passed / 4 failed，14.47秒 | 4项在旧代码9e106bc3同样失败，非本次引入 |
| 旧代码定向失败对照 | 相同4 failed，1.17秒 | 旧loop/store在独立进程加载，不覆盖共享文件 |
| worker恢复P2与相邻恢复 | 8 passed，4.25秒 | 双MemoryError、缓存一致/缺失/改写；0真实模型/MCP |
| 正式产品配置入口 | 首批12 passed，43.12秒 | UTF8产品环境；最终窄修后复验另记 |

各批有重叠，不相加为独立用例总数。4项既有失败分别为StopIteration异常身份、两个context limit旧预期、reasoning估算hook旧预期；保留证据，不修改无关产品行为来消除红项。最初root pytest因basetemp父目录未建在setup失败，建目录后有效通过；登记完成前一次verify及3项observer被版本守卫拒绝，登记后verify和88项有效通过，不放松封印。

最终worker/入口合批初次44 passed / 1 failed，42.07秒：严格整份receipt判等误将正式入口合法追加的worker统计视为漂移，导致已完成任务恢复时重复写日志。修为核验全部journal原始字段并只返回journal回执，追加统计由调用方重建；新增纯runtime回归已通过，失败的正式入口定向复验1 passed / 36.81秒，完整交付及恢复无新增请求/事件。合批45个不同用例均有有效通过结果。这是本次发现并修正的回归，不归到既有4项失败。

独立审阅及修后窄复核无剩余P1/P2，最终入口复验通过后冻结再启动真实复测。原双故障独立复现修后证据在 `D:/Sim-BIM-Agent/cache/memory-worker-resume-independent-pr5yrev4/probe-results.json`；附加报告兼容改动已另作独立静态复核。外部证据根 `D:/Sim-BIM-Agent/cache/mesh-repair-20261019/`，包括资源归属报告、旧代码对照、定向JUnit及新运行控制器；失败002原件保持。

## 修后真实复测

准备使用 `gpt-6.1-sol/high`、原mesh/23F/中文说明、48请求/240工具、每响应本地输出预留32000tokens、最多8个同模型只读worker，无整案seconds/tokens截止。新运行目录：`D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-repaired-20261019-001`；审计目录：`D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/61sol-repaired-20261019/`。

本段在代码冻结时为**待启动**，实际结果在运行终态和独立评价后补齐。离线修复通过不代表整案交付或提速通过。
