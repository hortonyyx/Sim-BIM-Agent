# 修复后真实读图长请求复测

用户要求用之前断掉的读图任务再测 Go 与 Paratera，mesh 保持不动。2026-10-19 12:40–13:01（UTC+8）完成四个串行冷启动，全部终态，没有自动追加重试。**Go 仍复现等待响应头时断连；Paratera 本轮没有提前断连，成功返回最长 234 秒的响应，最后耗尽节点时间预算。四个节点均未正式提交。**

## 固定条件

- 代码 `4ddd8836`，runtime30/domain113。使用已完成的 Go 分协议路径与 Paratera 图片参数修复，本轮未改程序、模型指导、代理、缓存、重试或超时设置。
- GLM 首层平面：Go `glm-5.3-flash` 与 Paratera `GLM-5.3-Flash` 各一次，`plan_reader/high`。MiMo Go `mimo-v2.6-flash`：首层 `plan_reader` 和北立面 `elevation_reader` 各一次，服务默认思考/采样。
- 每节点 900 秒、32 请求、128 工具、32,000 输出 tokens、0 自动重试；沿用旧配置的无额外总 token/金额上限。串行、最多一个 reader，产品默认并发保持不变。单请求通常上限 450 秒，仍受节点剩余时间约束。
- 输入固定 `D:/Sim-BIM-Agent/cases/sm25-gpt-20261012/input`。四个旧配置 SHA 匹配，case 内仅改变 `output`；角色任务和统一坐标原文保持。六张原图的 SHA 与旧批次完全相同，未供给 GT 或旧模型答案，未进行人工修稿。
- 这是同任务的新冷启动，不是旧失败请求的逐字节重放；模型选择的观察和请求内容可以不同。被动观察器与旧串行批次逐字节相同，记录阶段、状态、时间和 wire SHA，不更改请求体、鉴权或路由。

## 结果

耗时为节点启动至退出，包括初始化、模型等待和工具调用。tokens 只列收到的供应商用量；未知不记作零消耗。

| 节点 | 耗时 | 请求/响应/工具 | 已知 tokens | 最终结果 |
|---|---:|---:|---:|---|
| GLM Go F1 | 141.328 s | 5/4/8 | 87,218 + 1 请求未知 | 第 5 请求等待 45.481 s 后 `RemoteProtocolError` |
| GLM Paratera F1 | 904.146 s | 6/5/9 | 132,612 + 1 请求未知 | 第 6 请求等 437.820 s 后被剩余时间预算取消 |
| MiMo Go F1 | 82.120 s | 1/0/0 | 无已知用量，1 请求未知 | 首请求等待 60.219 s 后 `RemoteProtocolError` |
| MiMo Go North | 141.916 s | 3/2/6 | 27,607 + 1 请求未知 | 第 3 请求等待 44.599 s 后 `RemoteProtocolError` |

合计 **15 请求、11 响应、23 工具，已知 247,437 tokens，另 4 请求 usage 未知**。0 正式提交、0 trial、0 有效读图稿、0 整案/mesh 运行、0 人工几何介入。完整费用未知；订阅与 Paratera 记账分开，图片预算估计不当成供应商实报 tokens。

| 行为/质量层级 | 本轮观察 |
|---|---|
| GLM Go | inputs 1、view_image 5、pixel_profile 2；未进入试建 |
| GLM Paratera | inputs 1、view_image 8；未进入试建 |
| MiMo Go F1 | 首次响应前断开，没有工具执行 |
| MiMo Go North | inputs 1、get_bim_reference 1、view_pixel_profile 4；未进入试建 |
| 图片/读图质量 | 无采用值净图、回叠或正式读图稿，几何、墙厚、开口宿主、连通及 5/10/30 cm 分档均未评 |
| BIM/查看器 | 未组装、未生成交付；没有变更或重新运行旧 mesh 候选 |

## 中断的区分

**Go：三个节点均在客户端发完请求体后，等待响应头时出现远端协议错误。** 实际全部使用 `/inference/go/openai/v1/chat/completions`，不是误走旧路径；没有 HTTP 状态、错误正文或 usage，均早于本地 450 秒上限。MiMo F1 是新连接的首请求也失败，因此不能只解释为连接池复用。现有证据仍不能在本机代理路径、Go 网关与上游之间定责，也没有证明统一固定的 60 秒截止。

旧 GLM Go 串行节点第 4 请求等待 62.497 秒断开；这次第 5 请求 45.481 秒断开。旧第 4 与新第 4/5 的 wire SHA 都不同，不能因新第 4 返回就称原失败 payload 已通过。本轮说明更正官方路径未消除真实读图断流；此前小图片闭环成功不外推为长任务稳定。

独立复核还发现三个 Go 节点各自的新旧首请求正文 SHA 相同：MiMo F1 旧首请求 200、新首请求 EOF；MiMo North 旧首请求 EOF、新首请求 200，随后新第 3 请求仍 EOF。相同正文存在成功与失败记录，不能把失败只归为正文必然不合法；但旧/新端点与时间不同，这组对照不能单独定位故障原因。后续请求正文已经分化。

**Paratera：本轮没有 HTTP 400 或远端 EOF，但仍未完成读图。** 前五请求分别在 7.582、30.758、93.670、75.244、234.329 秒返回 HTTP 200。第六请求发出时，runtime 记录的时间预留只有 **437.827403 秒**，小于通常的 450 秒；trace 在 437.820 秒出现 `asyncio.CancelledError`，父节点回执 elapsed 为 **900.610 秒**，最终 `child_time_budget_exhausted`。

因此这次是节点 900 秒总预算的剩余时间取消，不能写成再次完整触发 450 秒单请求上限，也不能与 Go 提前 EOF 混为一谈。最后请求仍没有响应/usage；超时后时间结算略超预留，原回执未形成其 usage settlement，汇总按逐请求/响应核对计入 1 次未知，不依赖 `original_missing_usage_requests` 单字段。

这一个 Paratera 样本证明当时可以等待数分钟并收到完整响应；仍不足以认定线路长期稳定，更不代表 GLM 已具备本任务的速度或交付能力。本轮未延长预算、添加重试、切换网络或引入其他模型。

## 核验与证据

- 四个节点的源码、配置与输入前后哈希一致；运行时间区间无重叠。所有 **15/15** 请求的被动 trace body SHA 与 journal wire SHA 一致。
- 没有新程序改动，未重复上一轮离线回归。独立审阅者已重建并核对全部15个请求正文SHA，核验配置可比性、Go失败阶段、端点、缺失usage和Paratera剩余预算，汇总无误。Paratera发送图片提示均为high，MiMo发送图片均省略detail；没有HTTP错误或模型格式失败。独审0模型调用、0产物改动。
- 运行根：`D:/Sim-BIM-Agent/runs/reader-connection-retest-20261019`，保留全部事件、blobs、回执及 `evidence-sha256.json`（1,288 个文件）。只声明本机证据，未声明远端大产物备份。
- 配置、启动器、被动 trace、前后哈希与汇总：`D:/Sim-BIM-Agent/cache/reader-connection-retest-20261019`。`batch-progress.json` 为 terminal；`audit.json` 为逐请求审计；`prior-comparison.json` 为旧批次输入/源码对照。

| 小索引 | SHA-256 |
|---|---|
| `batch-plan.json` | `973784fb90daa56d198400b153ac612727b7ac7b5fc0df3eccb6a85ce7ffd9f6` |
| `audit.json` | `eff529cbd0bea6ae0951b4626a7525eeffea7fe1bb70de257c58eb9124bcf921` |
| `prior-comparison.json` | `9a8d6c302e3f97ca9491f3ffd64301f0d962a6b59ba1a41660f2c0e4319efae5` |

前次记录：[连接修复与小型验收](2026-10-19_provider_connection_repair.md)、[修复前串行读图](2026-10-19_reader_transport_retry.md)。本轮不改其历史结果。开发版仍 runtime30/domain113，固定桌面仍 runtime24/domain110，未发布；没有新建工作树。收尾 C/D 剩余约 45.85/121.72 GiB。
