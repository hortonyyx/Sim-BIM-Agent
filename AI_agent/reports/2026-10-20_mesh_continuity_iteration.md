# 竖向连续性约束与检查补漏

用户授权补上连续性约束和检查漏项，尽量不增加模型往返，然后用原输入、原模型及预算完整跑一次。目标仍是总耗时、首稿后的收尾行为；不提高 Lite 几何精度，不增加档位、角色体系或中断恢复方案。

## 问题与范围

前轮 closeout001 的 3 电梯井、2 楼梯贯通，但 23 个 duct 空间按层封闭，22 个界面有完整水平封板。原模型甚至记录了“inferred vertical duct continuity”，却未落实到几何。旧机器检查未覆盖这种问题，独立审阅只从 `spanning_space_ids` 查 5 核心也漏掉管井；不能将它归为单纯尚未检查即中断。

旧首稿只读反例现全量枚举 28 个竖向语义空间并发现 22 个封闭界面，设备间和卫生间没有自动作为竖井。原 source/plan 未改。独立证据在 `D:/Sim-BIM-Agent/cache/mesh-continuity-independent-20261020`；产品检查对 168 空间扫描约 0.022 秒，结果在 `D:/Sim-BIM-Agent/cache/mesh-continuity-iteration-20261020/old-c01-product-regression.json`。这是单次本地检查，不是整案提速成绩。

## 实现

- 首次声明就采用 `continuous_spaces` 或连续体；指引明确涵盖管井、风井和中庭，不能只检查已经贯通的核心。已知独立栈一起声明或局改，复用现有构建/补丁工具。
- 共享源检查遍历全部空间，按相关角色、同 XY 轮廓和接壤 Z 发现栈，核对真实水平边界及经验证的 enclosure 开口；不以名称或任意文本推断连续意图。未决栈一次聚合反馈。
- `separated_spaces` 提供真实封隔的结构化表达，要求明确成员、理由和依据，编译为 `geometry.vertical_separations` 并保存。拒绝未知成员、重复界面、孤立声明、与连续声明矛盾及非物理封隔；不自动合并设备间。
- 仅新 mesh manifest 采用 `explicit_stack_v1`。通过现有 source severe、closeout 和 finish 检查阻止未决栈被直接交付；旧 drawing/旧 manifest 不静默增加硬拒绝。种子和 worker 的政策传递保持一致，没有新增模型检查工具或固定审查轮。
- 几何或 enclosure 更新后重算并替换本检查自己的 findings，已修接口不保留旧报错，无关严重问题仍保留。相邻 Z 与合并统一既有 `1e-7m` 数值容差，避免纳米级缝隙触发无效往返；未选空间不动。

范围仅为已声明相关角色、同轮廓、接壤高度的关系。变化轮廓、不同角色、真实高度间隙、未知用途及设备服务不因此获证；报告明确列出未覆盖项。机器“resolved”不是全建筑功能或独立质量通过。

## 验证与独立审阅

| 范围 | 结果 | 耗时 |
|---|---|---|
| 连续关系、封隔契约、保存/重读、局改和数值边界 | 44 项通过 | 4.52s |
| 第一批 mesh 构建、closeout、输入与完成门禁 | 80 项通过 | 21.71s |
| 最终构建至交付、enclosure、跨层、shape gate、worker evidence | 74 项通过 | 27.00s |
| 独审后真实 overlay 下未决交付拒绝→局改→交付 | 1 项通过 | 11.75s |
| 封版后的原生 runtime/MCP 与共享 worker 交付入口 | 1 项通过（scripted，非真实模型） | 48.57s |

相交批次不相加为唯一测试数。全部为无计费门禁下的离线验证，0 真实模型调用。首轮新增测试曾有 `set_notes` 夹具错误，修正后复验通过；不是产品运行失败。

独立审阅发现并关闭两项实质问题：无效封隔的具体诊断被泛化异常吞掉；enclosure 打开接口后旧 policy finding 未退休。另修正负例测试缺少真实 overlay 的假通过可能。最终无剩余 P1/P2。测试与审阅不证明真实建模必定一次选对关系。

登记版本：`domain-v130-20261020`，公共 runtime 保持 `runtime-v34-20261019`。本轮未改传输重试/中断机制，未更新桌面。

## 已授权的单次复测

配置 `D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/run-config.61sol-continuity.json` 相对 closeout001 仅改 batch/output。原 GLB、23F PNG、building.json、single_model、GPT-6.1 Sol/high、48 请求/240 工具、worker 上限 8、一次重试及其余预算保持。

原生冷启动目标 `D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-continuity-20261020-001`。不导入旧稿或旧上下文；不得将前次 continuation 的临时桥接作为此轮运行入口。完整统计 controller/runtime、首保存/首有效/finish、首稿后模型/工具与局改/主体修改、失败用量和独立连续性检查。当前本报告记录实施与离线验证，真实测试结果随后另记；20/15 分钟尚未证明。

## 同日复测结果补记

实现提交 `00cd63e3` 后已执行上述单次冷启动。7 个连续体在首稿落实，无连续性补修轮；首保存 11分19秒，局修后首机器有效 14分20秒。随后只读检查仍未完成，最后请求及一次重试均连接失败，0 finish。runtime 16分46秒不是交付时间，20/15 分钟仍未证实。完整过程、质量、用量和保护证据见[复测报告](2026-10-20_mesh_continuity_retest.md)。本次未追加恢复或重跑。
