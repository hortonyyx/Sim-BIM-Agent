# Textured mesh harness 首批实现

2026-10-19。用户授权开工，并再次确认公共底座共用，仅 textured mesh 处理方法暂时独立推进。依据[上一轮共识](2026-10-19_textured_mesh_harness_closeout.md)实施；本轮未启动真实模型、生成 mesh、训练 CV 或更新桌面。

## 实现与公共边界

- 公共 runtime 未变，仍 runtime31；最终登记 domain115。预算、模型连接、工具循环、恢复、源 BIM、kernel 与 viewer 共用，没有另建 mesh runtime 或第二种交付 BIM。中间 domain114 登记保留，不追改旧哈希。
- 正式配置新增 `auxiliary_images` 和 `building_input`，mesh GLB 可同时带辅助 PNG、UTF-8 JSON 声明；全部受同一 `input_root` 约束，按原字节冻结并记录哈希。声明内的路径只作文字，不加载未授权材料。纯图纸继续原默认规则，mesh 辅图不因 `image_kind=drawings` 自动启用整楼 10 cm 网格。
- Mesh 方法优先外体量、楼层关系与外窗；未知内部按合理性与用户需要表达，删除一窗组一房默认。图纸、关系草图及文字按各部分信息作用选择精度，用户确认、用户推测与模型假设分开。纯图纸 CORE 内容保持；共用几何参考新增显式多边形合同。
- `geometry.schema_version=source_polygon_v1` 通过 `simple_polygon` 能力合同进入原源 BIM builder。支持简单直边/凹多边形柱体、斜墙上的明确窗门宿主、各层轮廓、退台实际接触、跨层空间及显式开放边界。保存、局部重划、源几何哈希和 viewer 继续同一条实现。旧 schema/profile 没有放宽；新合同不允许静默丢掉 holes/curves/top_polygon 字段。
- 斜窗用 `geometry.openings` 的 `kind=window` 和明确 `p1/p2/z/space_id`；不生成通行连接。局部重划改变宿主后，旧门必须明确移到实际共享边，不能因编译失败自动改窗门。新合同不能绕过现有 manifest 网格检查。
- `overlay_mesh_candidate` 默认 `visibility=visible`，按保存源几何深度与外部宿主朝向显示，可用任意 `boundary_ids` 选择墙段；局部显示仍使用完整源模型遮挡。内部观察 `exterior_only=false` 采用双面深度，保留全部宿主与作用域记录。显式开放通道/门和 open enclosure 不遮挡，窗及未知/关闭门状态仍按包络遮挡。隐藏对象保留在完整记录，另报可见采样；`xray` 可显式显示背面。
- 正式 mesh 运行不再依赖图纸楼层清单才能结束。`finish_bim` 可携带 `reviews_json={"mesh_review":{"coverage":"whole_building","summary":"范围判断及未决","overlay_ids":["overlay_001"]}}`，未完成则声明 `partial`。当前全楼声明、源验证 `pass` 及已保存 viewer 才允许结束；缺失、局部、失效声明仍可交付草稿，但不会完成运行。声明绑定源模型/报告、原GLB、所有已准入PNG/文字声明/恢复计划、回叠及观察图的实际字节；交付新增第七件review哈希。范围仍为模型自报，`fidelity=not_evaluated`，不冒充独立质量。图纸原list review和floor completeness语义保留。

可见性只表示源模型几何，**没有对原 mesh 做自动拟合、精度判定或真实可见面匹配**。原 mesh 缺失也不等于开口。新[产品配置示例](../../product/config.mesh.example.json)复用 single_model：初版调度员自主选共用工具并亲自工作，尚未接入 mesh worker。现有图纸 role_division 保留原入口，不能把强制逐图读图流程套给草图。

## 验证与独立审阅

| 范围 | 结果 | 耗时 | 真实模型/用量 | 边界 |
|---|---|---|---|---|
| 首批10模块集成定向：混合准入、公共输入、斜边源BIM、可见性、真实MCP工具、guidance、配置/路径 | 158通过，0失败/跳过 | 51.42秒 | 0；无真实tokens/费用 | 完成条件扩展前的domain114实现，离线合成反例，不是完整历史测试全集 |
| domain115完成链路及相邻公共回归8模块 | 102通过，1失败 | 188.68秒 | 0 | 唯一失败为既有图纸门禁测试夹具缺少10/18要求的墙厚，实际提前被拒绝；修复后单独复验，不重跑已通过范围 |
| 上述图纸门禁模块夹具修正后复验 | 3通过，0失败/跳过 | 10.29秒 | 0 | 只补本例必填墙厚，不改公共example/产品；独审核对仍实际命中同一响应不得审批新图的原门禁 |
| 源几何实施阶段邻接回归 | 91通过；另一组54通过 | 6.12秒；9.86秒 | 0 | 范围含重叠，不能相加成独立总数；覆盖源接触、enclosure、spanning、proposal、局部修改及相邻kernel |
| 独立代码审阅及修复复核 | 无剩余P1/P2；可见性9项、完成链路49项通过 | 3.86秒；21.65秒 | 0 | 两处可见性、两处证据绑定P2全部修复复核；未评模型行为 |
| 版本登记与 verify | domain115 / runtime31；verify通过 | — | 0 | 仅domain变化 |
| 正式冻结入口 scripted smoke | `completed`、明确选择candidate_01，7件交付哈希和证据复验通过 | 13.11秒墙钟；receipt12.15秒 | 6脚本响应/6工具；0真实模型 | 合成三角柱GLB+草图+声明，由测试预先给出参数；不是自主质量或5–10分钟目标实测 |

首批定向证据：`D:/Sim-BIM-Agent/cache/pytest/mesh-harness-final/4a6f775c5dea`；完成链路及公共回归：`D:/Sim-BIM-Agent/cache/pytest/mesh-completion-final/1de263a74e88`。独立反例：开放通道后窗由0/304恢复304/304可见；内部门从两侧、两个宿主作用域均524/524可见。相关最小反例已加入测试。早期失败包括 Windows 未激活 UTF-8 的既有测试读写、并发编辑期间实现哈希变化，以及新增夹具问题；按标准 UTF-8 环境并在文件稳定后复验。各阶段/独审测试有重叠，不能直接相加。

图纸夹具复验JUnit：`D:/Sim-BIM-Agent/cache/mesh-role-delivery-fixture-20261019.xml`。事件确认 `trial_plan_bim=succeeded`，随后同响应 `submit_plan_reading=failed`，原因是实际trial图尚未进入模型请求；未生成正式提交。原102个通过结果继续有效，唯一失败已修复复验，无遗留失败。

真实 MCP 集成已覆盖新多边形 `build_bim → view_mesh → 指定斜宿主 overlay → finish_bim`，确认实际保存的 source/viewer/交付哈希贯通；输入为合成夹具且建模参数由开发测试给定，不是自主建模质量证据。正式 runtime 的成功脚本验证在 `D:/Sim-BIM-Agent/cache/mesh-harness-20261019/native-smoke-complete`，摘要 `native-smoke-complete-summary.json`。实际floor completeness为null，mesh scope为current/whole_building；终态保留`scope_basis=mesh_model_reported_review`、`completion_valid=true`和`fidelity=not_evaluated`。人工看过合成回叠，未做真实案例视觉验收。

首次 `native-smoke` 因脚本测试的保守图片token预留超过200万而停在保存候选后，未执行overlay/finish，保留为失败证据，不计完成。随后另建新目录，将仅用于脚本的预算调为2000万，并修复检查时发现的纯mesh完成条件缺口。成功脚本的usage每响应固定20、累计120，只是模拟记账，不是真实服务token或费用。原失败回执未覆盖。

## 未完成能力与下一步

- 尚未实现真曲线、带孔空间、倾斜墙面、同一空间沿高度变截面，也未给新几何合同接通 EnergyPlus。参数化模板、轴向墙线编辑和四方向图纸立面接口仍有原能力边界。
- 任意墙宿主回叠解决观察定位的一部分；平立面自动匹配、原 mesh 表面与源宿主的完整证据绑定仍未完成。
- Mesh worker 需要扩展现有子任务证据包和结果绑定；目前只证明调度员可直接操作共用工具，不宣称多worker自主调度已实现。
- 下一步选贴图完整的单栋案例，固定辅助信息与质量口径；真实测试前列明角色、型号/思考档、输入、预算及验收范围，按当轮授权启动。5–10分钟/常规15分钟仍是目标，没有本轮实测结果。
- 开发 runtime31/domain115；固定桌面仍 runtime24/domain110，未发布。旧案例、旧实验与旧工作树未修改或清理。
- 收尾 `verify`、diff检查通过；445个domain文件和46个runtime文件共491项与Git暂存字节哈希一致，避免Windows换行导致封印漂移。无新增工作树需清理；磁盘快照C余34.88 GiB、D余121.14 GiB。
