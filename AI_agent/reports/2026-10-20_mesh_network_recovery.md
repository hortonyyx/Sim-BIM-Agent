# 断线恢复与第二稿续跑

用户明确要求补上断线重连和断点续跑、单独记录断连时间，然后从 continuity001 保存的第二稿继续到交付，不重新冷启动。继续采用已认可的 Lite 质量范围，不新增精度、档位或角色体系。

## 实现

开发版最终登记为 `runtime-v36-20261020` / `domain-v131-20261020`；runtime35 是增加最后截断准入保护前的登记，未用于真实续跑。历史版本记录内容保持。

- 公共 runtime 增加显式、累计有上限的网络恢复预算。本次配置为 180 秒；默认零仍保持原重试策略。父流程和 worker 共享恢复记账，恢复检查点后不重新获得额度；失败调用仍占原请求预算。
- 网络恢复先等待再重试；正式 HTTP adapter 只在连接及响应头阶段应用恢复窗口，已建立的正常生成流保留原 IO 超时。权限、输入、工具权限、全案预算等失败不按网络故障无限重试。
- 持久事件分别记录失败请求耗时、恢复等待和连接阶段占用。失败流可能已经包含模型生成，不能把全部失败调用时间称为纯断网时间；修复开发期间的停机间隔也独立列出。
- mesh/single_model 新增正式 continuation：校验原事件、检查点、全树及模型/工具/输入身份，在新目录保留原稿、完整上下文、图像和已执行工具结果。旧 run 不改，旧修改不重放，耗尽预算且无结论的 worker 不变成已完成意见。
- 新版本恢复必须显式允许恢复策略变更；建筑提示、几何逻辑和模型配置保持。继续扣除此前使用量，同一续跑再次 resume 不清零。

## 离线验证和审阅

| 范围 | 结果 | 耗时 | 真实模型 |
|---|---:|---:|---:|
| 配置、角色入口兼容、带 continuation 配置同 run resume | 59 通过 | 15.00 秒 | 0 |
| 网络恢复、服务失败、HTTP responses、时间结算 | 126 通过 | 14.65 秒 | 0 |
| continuation 和精确上下文：含 0/1 worker 原生 MCP 中断→导入→正式 finish→同 run resume | 23 通过 | 127.84 秒 | 0 |
| 不允许通过新 run 重置尚未结束的连续截断保护 | 1 通过 | 1.81 秒 | 0 |

这是受影响模块验证，不是全部历史测试，也不代表真实网络或建筑效果通过。独立审阅发现的版本身份接线、恢复入口、共享恢复额度和连续截断保护问题均已修复并复核。处于截断恢复中的旧 run 不在本次跨版本网络 continuation 准入范围；本案没有截断。

证据分别为 `cache/mesh-network-recovery-20261020/config-final/dbd83b8f72f8/results.xml`、`cache/runtime-reconnect-20261020/618681dee19f/results.xml`、`cache/mesh-resume-final-20261020/ab21c58883e7/results.xml` 和 `cache/mesh-resume-final-20261020/2ada6a7ed82a/results.xml`（均位于 `D:/Sim-BIM-Agent` 下）。早期配置入口重复注册参数和迁移版本读写接口错误已修复；Windows 瞬时失败计时测试改用明确延迟，旧时间结算夹具补齐 completion 字段。最终结果只覆盖各批列出的模块。

真实源只读准入确认：2881 个文件、83 条历史、51 个工具结果、41 张图像和 15 条状态均有绑定；剩余为 25 次模型请求 / 183 次工具，seconds/tokens 仍沿用无上限配置。检查点没有待执行响应或未决修改。

## 真实续跑协议

- 原 run：`D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-continuity-20261020-001`。
- 原第二稿文件 SHA256：`847124bad118f31c9446c17e93e61ff69c7806ba47f82c7c067ee87b3af2f430`。
- 原事件 SHA256：`92ecea40a58a67901a8d28b5a9a0acb24d650a75544f7d40993b9c1a9b6b7c8f`。
- 目标：`D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-continuity-20261020-001-continuation-001`。
- 配置：`D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/run-config.61sol-continuity-resume.json`。
- 资源、配置封印和控制器审计：同级 `61sol-continuity-resume-20261020`。
- GPT-6.1 Sol/high，保留原上下文和输入；不人工补建筑提示或改几何，不新建模冷启动。正式入口 `sim_bim.py run`，启动沿用 8 GiB 可用提交内存条件。
- 完成必须同时验证实际 `finish_bim`、有效 completion 及 complete_building；故障 fallback 打包不是正式交付。

实际续跑尚待版本冻结后执行。原冷启动运行 16 分 46 秒是中断点；应在续跑后分别报告此前活动时间、本段活动时间、失败请求/恢复等待、离线修复间隔，不把拼接结果称为一次不中断冷启动成绩。
