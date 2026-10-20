# 局部收尾迭代后单次冷启动：连接失败，首稿保留

用户“好，测一次”后，以 b5257ad5 / domain129 / runtime34、原三输入、GPT-6.1 Sol/high 和原预算冷启动一次。仅保存 candidate_01，首稿在18分17秒通过机器检查；运行随后因响应流中断、一次自动重试连接失败结束，未执行 finish_bim。**本轮未正式完成交付，不能把23分46秒故障退出当作提速成功。**

## 范围与终态

| 项目 | 本轮结果 |
|---|---|
| 冻结版本 | b5257ad57d1d1ce6fcf43bc62bcd8137cb7c5d80；domain-v129-20261020 / runtime-v34-20261019 |
| 模型 / 入口 | single_model、chatgpt-subscription、gpt-6.1-sol、high |
| 输入 | 原 building.glb、building.json、auxiliary/23f.png；配置仅改batch_id与output |
| 预算 | 48请求、240工具、单次输出32000、64候选、1次模型重试；无整案时间/token硬截止；image history 3、reasoning history all |
| 可选worker | 上限8沿用；实际1个facade_rhythm，4请求/7工具，151.971秒，已回交且主模型引用 |
| 状态 | controller exit1；receipt model_retries_exhausted:transport_error；0 finish、completion=null、complete_building=false |
| 保存结果 | candidate_01；168空间、1526边界、278开口（46窗、232非窗）、232连接；0未建/unsupported |
| 时间 | runtime1425.740秒（23分46秒）；命令1461.769秒（24分22秒）；正式交付耗时不可用 |
| 请求 / 响应 / 工具 | 全案25/23/64；父21/19/57、worker4/4/7 |
| 已知用量 | input1,786,346 + output31,578 = 1,817,924 tokens；另2次失败usage未知，全案用量不完整 |
| 费用 / 干预 | 订阅货币费用未知；0人工改稿、0补提示、0额外模型追问、未追加第二轮 |
| 独立评价 | 过程审计、保存首稿视觉/语义及固定数值评价分别报告；0评价模型调用 |

运行从2026-10-20 18:44:56至19:09:18（新加坡时间）。配置、doctor、版本、原输入allowlist及独立就绪检查在0模型时通过。启动前可用提交内存18.715GiB，沿用8GiB启动条件，未终止其他会话。484个3秒资源样本有效，最低可用提交约8.68GiB、最低物理约6.30GiB，子进程采样私有内存峰值约3.77GiB；无MemoryError记录。这些为采样值，不是精确峰值。

receipt 的 `finalization.status=delivered` 只表示打包了保留产物；delivery 明确 `latest_saved_fallback_not_agent_selected`、`generation.state=interrupted`。没有 delivery_selection.json、completion或整楼mesh_review，不能混同工作模型完成。

## 速度：首稿与收尾分开

| 时点或区间（runtime起点） | 本轮closeout | 上轮serial |
|---|---:|---:|
| 首构建返回 | 11分56秒 | 12分38秒 |
| 首保存候选 | 18分17秒，c01 | 19分02秒，c01 |
| 首机器有效候选 | 18分17秒，c01 | 36分01秒，c04 |
| 首构建→首保存 | 6分21秒 | 约6分24秒 |
| 首保存→正式finish | 不可用 | 36分24秒 |

首保存仅早45.200秒，首机器有效早1064.296秒。旧serial首稿外观/关系已获用户认可，不应因其旧机器拒绝而删掉首稿时间；本轮机器有效也不自动等于用户认可。没有正式收尾终点，不能将失败总时长与旧成功总时长相除，也不能把较少tokens称为等价整案成本下降。

首构建到保存经历5次build/patch，只有1次保存有效。d01轮廓规整拒绝4项：基座近共线支持线交点移动3.387m、屋顶移动0.104m；随后一次patch下标越界。d02接触检查报告办公空间/第三电梯从侧接触变为体积重叠；d03用30项操作引入局部轴、改写空间/核心/门窗后，前窗带找不到完整原始直线宿主。最后4操作把前缘端点X移动约3.865mm，d04保存c01。

独立直线距离复核d03的两窗端点距最近宿主约0.097mm/1.490mm，确认很小的不共线误差引起了一轮强拒绝；不能据此把较大交点放大或接触改变也认定误报。381.212秒返工区间包含6完整响应、351.202秒模型调用，主要成本仍是模型往返。不同阶段逐次暴露阻断，不是同一错误无修改地循环。

首保存后仅观察到101.177秒正常检查：2父响应、12工具、0修订、无第二稿。已返回的closeout正确显示机器clear、4项unknown未自动接受、mesh_review missing。随后一次响应流失败耗时208.796秒，重试连接失败5.015秒；失败调用合计213.811秒。不能扣掉失败时间后宣称正常完成能达20分钟，因为最终判断与finish均未发生。

本轮直接在首稿声明连续核心，使用5条core_connections，continuous_spaces=0；新增局部贯通补丁没有被真实执行。单个worker虽回交且被引用，父child wait仍151.971秒，没有证据证明已获得并行提速。

## 保存首稿质量

独立实际查看原23F、c01图纸回叠、G及23F平面、整栋/屋顶/后立面回叠及商业底部/上部窗近景，并逐门核对源几何。

- 23服务层的69电梯门、46楼梯门逐一通过双宿主、接触、包含、连接与本层标高检查；5核心均为真实矩形，0–75.6m连续，只有顶底水平边界，无其他水平面穿入形成中间封板。
- G/L1商业基座单独识别，主要凹进和入口表达成立；未见此前“未识别底层/奇形走廊”问题。23F折翼、三电梯、两楼梯及主要办公组织可识别，局部前室/边界仍有偏移，不从无标尺图推算米制精度。
- 上三排框窗向上缩短、内侧玻璃带扩大有明确原图依据；c01仍使用下部统一长窗带，属于应披露或成组局改的已知偏差。它不要求重做主体，也不能仅因为记入unknown就自动接受。
- 后窗完整性、其他层真实内部、屋顶通达性仍未证实；所有连接状态为unknown。屋顶栏杆/设备细部按声明简化，当前不追加精修目标。本检查不证明梯段、电梯运行或疏散合规，不替用户给新稿全面OK。

独立源重建validation/precision通过，168空间、1526边界、232非窗开口和46窗ID及几何保持，仅generation来源与规范SHA变化。冻结评价算法、九视角及每视角16×16射线均沿用，直接使用工作模型保存的坐标系、不拟合，共2304射线。

| 独立数值口径 | 结果 |
|---|---|
| 本轮保存首稿有效样本 | 1211/2304，九视角全完成、0评价失败 |
| 本轮≤5/10/30cm（占1211条有效样本） | 20.727% / 58.629% / 87.036%，均差0.28931m |
| 与serial最终稿共同1172射线 | 均差0.23111→0.29532m；≤10cm86.775%→58.191%；≤30cm94.198%→86.775% |
| 共同主体1047射线 | 均差7.270→13.641cm |
| 共同屋顶125射线 | 均差1.558→1.626m |
| 评价耗时与保护 | 113.849秒、0模型；原run2878文件及总3080项保护文件保持 |

同视线深度一致性较上轮最终稿退步，不能称质量保持已被全面证明。同时，这是保存首稿与历史完成稿的描述对照，阶段不同；5/10/30cm不是窗端点精度、外表面积覆盖率或用户Lite验收阈值，不替代实际图像判断。未命中/边缘样本不能当作0误差，区域分界沿用旧协议，仅用于诊断。

## 保护、证据与下一步

终态source规范SHA有效，启动提交/运行版本一致，3输入哈希保持。过程审计前后2878个run文件清单/SHA一致；视觉检查连同3输入共2881文件保持。原历史候选没有被改写，评价产物全部写 sibling evaluation。本轮仅记录测试与独评，产品代码、档位、角色和预算均未修改，桌面未更新。

主报告的过程、数值、视觉/功能摘要分别经对应独立审阅者核对通过；仅澄清数值百分比分母，未为文档重跑产品检查。未创建工作树；收尾前C盘43.99GiB、D盘115.05GiB（检查时值）。

- [保存首稿viewer](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001/bim/candidate_01/viewer.html)、[23F回叠](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001/bim/image_overlays/overlay_001.png)、[整栋回叠](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001/bim/mesh_overlays/overlay_001.png)
- [终态完整性](D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/61sol-closeout-20261020/terminal-integrity.json)、[controller](D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/61sol-closeout-20261020/controller.json)、[资源](D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/61sol-closeout-20261020/resource-summary.json)
- [独立过程报告](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001-evaluation/process-comparison/ANALYSIS.md)、[机器事实](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001-evaluation/process-comparison/verified-facts.json)
- [独立视觉/功能报告](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001-evaluation/visual-review/REVIEW.md)、[层门和贯通核查](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001-evaluation/visual-review/candidate_01-semantic-audit.json)
- [冻结数值协议](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001-evaluation/numeric/numeric_protocol.json)、[失败终态评价绑定](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001-evaluation/numeric/numeric_execution_binding.json)
- [数值结论](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001-evaluation/numeric/results-01/independent_numeric_verdict.json)、[数值执行与保护](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-20261020-001-evaluation/numeric/numeric_execution_result.json)

后续仍优先缩减首稿前的观察/声明与准入往返，尤其明确关系下的毫米级宿主处理、基本几何及共享参数采用；真实接触/大位移与表示误差分开诊断。首稿后保持主体、只做必要局改的方向继续保留，但本轮因服务故障未完成效果验证。先讨论下一次是否恢复或重测及所需范围，不自动追加模型调用、改harness或提高精度目标。
