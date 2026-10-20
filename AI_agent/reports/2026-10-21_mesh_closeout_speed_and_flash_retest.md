# 收尾提速复测与 DeepSeek Flash 独立整案

用户授权先同条件完整复测；若结果可接受，无需再次确认，再让最新 DeepSeek Flash 独立完成一次。两轮均已正式 finish，未发生网络中断。Sol 必要质量独评通过，Flash 必要质量独评失败。没有修改产品、人工修稿、人工追加提示、预算扩容、续跑旧稿或发布桌面。

冻结产品为 `2c04298e1b8c28280724af9e8b07f3a6eeb8c822`，runtime-v36-20261020 / domain-v132-20261020。测试期间工作树干净；本次提交仅更新交接文档。批次于本地 10 月 20 日启动、21 日完成，运行目录保留原批次日期。

## 结果

| 指标 | GPT-6.1 Sol / high | DeepSeek Flash / thinking high |
|---|---:|---:|
| 正式交付 | candidate_03 | candidate_04 |
| 独立必要质量 | 通过，符合本案 Lite 范围 | 不通过，重要关系丢失 |
| Runtime 完整耗时 | 20分40.7秒 | 25分36.7秒 |
| 控制器总耗时 | 21分13.3秒 | 25分49.2秒 |
| 首次保存 | 7分41.4秒，已机器有效 | 15分06.4秒，仍有阻断 |
| 首机器有效稿 | 7分41.4秒 | 24分58.6秒 |
| 首机器有效后至终态 | 12分59.3秒 | 38.1秒 |
| 最终保存至 finish | 86.0秒 | 36.4秒 |
| 模型请求 / 响应 | 21 / 21 | 48 / 48 |
| 工具调用 / worker 请求 | 61 / 0 | 57 / 0 |
| 全案实报 tokens | 2,458,023 | 11,040,881 |
| 网络失败 / 重连等待 | 0 / 0秒 | 0 / 0秒 |
| 输出截断 | 0 | 2 次，各 32k |
| 费用 | 订阅，未编造按量费用 | 估算 US$0.46649，非账单 |

两轮 receipt 都是 `completed`、`completion_valid=true`、`complete_building=true`，正式完成绑定的 7 份产物哈希逐份通过。Flash 恰好在第 48 次请求成功 finish，不是预算耗尽后的 fallback。正式完成、机器源合法和独立质量是不同结论。

### Sol：总时间接近20分钟，但有效稿后的整体耗时未改善

与此前原段加恢复段的 25分42.3秒活动时间相比，本轮总时间少 5分01.6秒；两者是不同执行轨迹，历史离线修复空档已排除，不是严格单变量实验。15分钟目标未达。

首机器有效稿提前至 7分41秒；有效稿后仍有 **10 个模型回合、27 次工具调用，约13分钟**，比前次约11分22秒更长。不能把总时间改善归因于收尾整体变快。

新能力实际被采用：

- 三次成功保存都返回 `edit_route` 与 `save_facts`；随保存自动返回 13 张当前稿叠图，另有三次 `view_mesh(candidate=...)` 同时返回叠图。
- 无单独 inference 审计或登记调用；最终 6 条解释随 finish 合批提交。
- 116 个未登记用途证据的空间没有触发强制补录或整体重做。
- 最终 c03 保存后只有一轮模型、一次 finish，交付手续已经集中。

尚存主要耗时：

1. c01 后两次失败修订的模型时间合计 **290.673秒**。第一次选择了正确参数编辑工具和哈希，却对不存在的根 `/connections` 使用 replace；回执只指出路径不存在，没有直接给当前根键及 add/replace 建议。第二次新增窗使用了规整后的坐标，与原始声明宿主不一致；随后修约1cm的声明坐标才保存。该时间不能全额视作未来可省时间，但定位了优先减少的返工。
2. c02 后约4分36秒用于底层近景、剖切、分页读取和修订，确有几何产出。G/R1 两层边界最大变化约2.46m/1.90m，净面积分别减少15.98/13.70平方米，并修改入口、侧窗与侧门。塔楼和核心的严格字段差异仅为约 1e-10m 的浮点重算，未实质改形。
3. 西楼梯平台和上三层窗也有实际修改。不能将所有首稿后检查都视作无效；下一步应优先降低局修返工、前移关键底层观察，并压缩没有新增决策的材料读取。

Sol 独评全扫 7 个连续核心：三电梯、两楼梯、风井和管井均保留独立矩形身份，0内部封板、0遗漏竖向候选；23服务层的69电梯门和46楼梯门、全部326开口的宿主/接触/包含/连接通过。当前 c03 的23F、底层、主要立面与屋顶实际图像共10张审阅通过。未把其他层推断、隐蔽开口或精细屋面设备标为已测量还原。

### Flash：流程跑通，质量与速度均未达到替代目标

采用官方 `deepseek` 路由、API 别名 `deepseek-flash`。启动时官方文档对应 **DeepSeek-V4.1-Flash-1017**；该别名可移动，不声称锁定不可变权重。官方 [型号与价格](https://api-docs.deepseek.com/quick_start/pricing/)、[更新日志](https://api-docs.deepseek.com/updates/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)已核验。

Flash 从同一组三个原始输入冷启动，没有收到 Sol 候选、执行上下文、修订答案或独立评价。开始时可用提交内存13.799GiB；同产品、同48请求/240工具、64候选、180秒恢复预算、原 scope 和3批图像历史。没有执行工作模型 worker。

前两次长响应均 `finish_reason=length`，32,000 completion tokens 全部为 reasoning，0文字内容、0工具；分别135.922与128.063秒，合263.985秒。Runtime 记录两次截断并按现有有界逻辑继续，丢弃不完整 thinking，不执行未完成工具。随后第15轮又耗133.797秒才发出参考读取，最终能够恢复推进。这不是网络断线。

**32k 配置不等于两家相同的实际输出限制：** Flash 请求明确发送 `max_tokens=32000`，覆盖思考加可见输出；Sol 订阅 wire 没有 max_tokens/max_output_tokens，32k仅作本地输出预留。因此不能把本轮称作等效输出上限下的模型能力排行榜，也不能据单样本推断其他预算下的 Flash 上限。

首次构建在10分49.5秒，之后反复处理门窗模板、声明轮廓与几何冲突；前三个保存稿都有机器阻断。最终通过继续简化得到一个合法源：

- 只有一个贯通服务块，合并三部电梯、楼梯、管井/风井等不同系统；23个服务层各一扇门。
- 最终没有外门；已画的230个开口合法，不能证明缺失的街层入口及原独立核心通达关系成立。
- 原23F中的第二楼梯、独立设备/电表、卫生间等关系被省略，核心变成东侧的大斜块。
- 屋顶多级退台被合为整轮廓6.4m高的 plant 盒子，实际回叠可见窗带和层线错位。

质量失败的依据是原输入中关键身份、位置和空间关系丢失，**不是空间数少，也不是提高几何精度要求**。一个大井贯通不能证明原图中各独立竖向系统成立。当前源合法性与连续性检查能验证模型已声明的对象，但不能仅据此证明输入中的必要对象没有被省略或合并；本轮由独立质量审阅检出了这一边界。后续可讨论最小的关系保留约束，本次不预设或实施修复。

过程共22次业务错误：build7、patch8、revise2、finish4、record1。前两次finish因源仍有阻断拒绝；c04之后另两次因inference字段及不存在的opening引用拒绝，最终第五次finish成功。

其中连续5次`missing fields ['source_refs']`只给字段名，没有具体嵌套对象路径，模型在不同位置试改后仍失败。更精确的对象路径及可执行局部修正，是两模型共同暴露的反馈改进方向；本轮未修改校验器。

早期合法性失败需要保留尺度边界：c01/c02每层约323.794平方米的轮廓，missing约0.014263平方米、outside约0.006985平方米，各影响23层；c03每层outside约0.001944平方米。这是小面积覆盖不一致，不是已证明的大体量错误；尚未追溯规整根因，不称误报。模型的解决却不是仅修小缝：c03→c04服务块面积85.873→41.866平方米，内部边界最大变化约7.281m，外轮廓几乎不变。c01/c02本来也只保留合并后的lift/duct/stair三栈，没有完整恢复原图7个系统。证据支持“小量合法性问题造成多轮返工，模型又以重大内部简化求通过”，不支持“harness必然导致全部语义退步”。

代码边界：`source_vertical_continuity.py`扫描已保存的`source.spaces`并检查同角色相邻栈；最终单shaft真实贯通，报告resolved没有算错。`mesh_review.py`校验当前源可用、声明结构与证据hash，再据当前`whole_building`声明生成完成范围；不会从原图独立重新识别各必要系统。`closeout.py`虽提示省略仍须按scope判断，但尚无输入必要身份/关系清单与最终对象逐项对照。这是可定位的语义覆盖缺口，合法性与绑定能力仍有效。

48次 usage 均完整：input10,691,367，output349,514，total11,040,881；cache hit9,162,752、miss1,528,615，reasoning278,063已包含在output。全部请求处于UTC16时非高峰，按官方每百万命中/未命中/输出 US$0.003/0.15/0.60 估算 **US$0.466488906**；缓存写入未报告，保持未知，不计为0。不是供应商账单，图片与思考不重复加算。

## 冻结数值诊断

沿用同9视角、每视角16×16、原27份观察与4个评价模块，无重建、无对齐拟合。固定射线不是内部拓扑或全楼保真的证明。

| 样本范围 | ≤5cm | ≤10cm | ≤30cm |
|---|---:|---:|---:|
| Sol c03：1196有效 | 24.33% | 77.42% | 93.90% |
| Flash c04：1257有效 | 42.32% | 69.85% | 80.83% |
| 两轮共同1195样本：Sol | 24.27% | 77.41% | 93.89% |
| 两轮共同1195样本：Flash | 42.18% | 70.88% | 82.34% |

共同样本平均绝对深度残差 Sol0.2206m、Flash0.6091m。Flash有更多近贴合样本，但大误差尾部更重，不能单看5cm比例判其更好。Sol与旧c04共同样本的10cm比例从82.53%降至77.35%，30cm从94.66%至94.06%；未以此上调Lite门槛或声称整体提质。

Sol诊断45.87秒，Flash14.64秒；这些是终态后的独立评价时间，不计入模型整案耗时。

## 证据与交接

- Sol run：`D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-speed-20261020-001`
- Flash run：`D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-deepseek-flash-closeout-speed-20261020-001`
- [Sol查看器](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-speed-20261020-001/bim/delivery.html)、[Flash查看器，独评不通过](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-deepseek-flash-closeout-speed-20261020-001/bim/delivery.html)
- [Sol过程独审](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-speed-20261020-001-evaluation/process/ANALYSIS.md)、[质量独评](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-speed-20261020-001-evaluation/quality/REVIEW.md)、[数值](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-closeout-speed-20261020-001-evaluation/numeric/summary.json)
- [Flash过程独审](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-deepseek-flash-closeout-speed-20261020-001-evaluation/process/ANALYSIS.md)、[质量独评](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-deepseek-flash-closeout-speed-20261020-001-evaluation/quality/REVIEW.md)、[数值](D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-deepseek-flash-closeout-speed-20261020-001-evaluation/numeric/summary.json)
- 两轮配置/预检/控制器及Flash质量启动条件：`D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019/audit/` 下对应 `61sol-closeout-speed-20261020` / `deepseek-flash-closeout-speed-20261020`。

Sol source file SHA256 `160116fed1f7540ad79324b70bec3d5b1e0ff17d9a42db87db4624edc2db245e`；Flash `2e09a68bf4cd1a4cfce15a362be9650a61e0183e38d03ff5b5cb095f20999234`。评价前后 Flash3447、Sol3383、历史续跑3297文件以及输入/评价依赖，共10181个保护文件哈希保持。质量审阅和数值诊断均为0工作模型调用。

两轮controller核验原输入与受保护旧配置保持，Flash另核验Sol整树保持。每3秒资源采样最低可用提交内存Sol约6.76GiB、Flash约10.32GiB，无MemoryError；8GiB是启动准入条件而非运行时强制退出线。采样不是精确峰值，后台任务波动没有被归因成模型故障。收尾磁盘约C42.02GiB/D112.39GiB可用。

本轮只运行获准的两次整案及只读独评；没有再追问工作模型、重跑失败阶段、调输出预算或追加产品测试。自动重连额度本轮未使用，不能宣称真实断网恢复再次实证。串行仍为主，档位及角色体系保持待讨论。下一步优先讨论局修可靠表达与必要关系防丢，不因本次结果自动换主线模型或展开大重构。
