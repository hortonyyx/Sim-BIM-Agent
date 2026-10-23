# Mesh 夜间迭代与验收

本轮按用户夜间授权继续交付档位之外的 harness，所有新增真实测试仅 GPT-6.1 Sol/high。分工保持探索，09:00（Asia/Singapore）汇报后讨论。当前恢复根为 `D:/Sim-BIM-Agent/cases/mesh-harness-overnight-20261024/`，先看 `handoff.json`、`PROGRESS.md` 和各子目录实际 controller/seal，避免重复派发。本线程定时续跑为 automation `mesh-harness`。

## 最新结论与当前恢复点

03:19用户再次明确持续推进后，已进一步合入小剖面完整预览和参数祖先/源修订历史提示，登记 **runtime42/domain146**；两项均完成独立代码审阅，源码897d7610已推送。真实Sol父子/单体固定ROI对照与两版谱系窄协议均已终态、完成独立复核；v1修改失败保留，修正文档可达性后的v2完成当前稿局修并保留旧改。所有终态封印已核验，当前可解除源码冻结；没有活动工作模型或本轮待合入源改动。

续作新增 **18次真实Sol/high请求、72,930实报tokens**；连同前批共 **99次、7,827,064 tokens**，涵盖2轮利园和12腿窄测试，不是此前十案横评的请求总数。旧18次脚本父请求另列，离线ScriptedAdapter不计真实调用；货币及Codex开发/评审用量未知。09:00早报继续，后续若有新探索必须重新冻结合同并更新总账，不能将本检查点数字当未来最终量。

前批已完成两轮利园和八腿窄测试，实际版本分别列于下文。前批81次新增真实请求均为 GPT-6.1 Sol/high，实报全树 tokens 合计7,754,134，另有18次脚本父请求。此小计仅含本报告七组已结束实验，不含后续、此前10案、旧ROI探针或Codex开发/评审推理；订阅货币成本未知。worker回执去重、主控量测摘要、共享角点定位和原声明字段反馈已合入，真实窄协议可用；**整案没有稳定提速证据**。

最新Lee002显式交付C06，24:48.55，较本夜001慢2:29.30；主控等待worker增加90.04秒是最大的分项增量。首有效稿后有实际外形改善，也有从旧参数基线恢复窗改的重复工作。最终紧容差外壳指标弱于001，已知23F前室/H.R.分隔损失仍在，不称全面提质。原run4669文件、输入及封印复核保持。

前批实际模型运行和独立复核已终态，其封印已解除；续作的新封印需另核。C04 `/connections/2/source_refs` 未在独立字段预检中定位、展开后报成 `/openings/234/source_refs` 的反馈缺口已独审并合入 `1922c9d9`，登记domain145。原失败回执现在并列给准确原声明路径；不是新增早期拒绝门，也不改变失败稿保留协议。新增窄Sol用时43.38秒/4请求，按三个原声明路径正确修复并完整读回；原审计30/31因嵌套读回范围过窄而失败，追加独立语义42项通过，原判定保留。参数衔接提示现已实施，仍不自动合并几何。档位与生产异构调度仍待讨论，09:00早报约定保持。

## 03:19续作：小剖面与参数谱系，domain146

小剖面源码 `b678e38f` 只改变view_mesh_section默认回执：完整segments按实际JSON编码≤8000字节、含remaining_seconds的完整文本≤12000字节时，一次展示原列表；否则保留原8条预览及既有全局报告约束。不是所有旧回执新增12KB上限。无新工具/参数、不改全局分页，原坐标、face_ids、索引、PNG和完整报告不变。Lee002四剖面纯内存重放：25/46条完整文本6525/9980 bytes，112/116条原样保持。8次旧分页来自3个模型批次、工具仅1.88秒，静态字节减少不是实测token或整案提速。

| 范围 | 结果与边界 |
|---|---|
| section定向与相邻 | 首次global Python无pytest，0测试；venv首轮19通过/1失败，失败是新合成GLB无纹理UV；修正fixture后仅该1项复跑通过/5.56秒。当前20个用例有成功覆盖，未冒称完整20项一次重跑。 |
| section独审 | 20项独立断言通过，含8000/8001和12000/12001精确边界、历史四剖面逐值不变；无阻断，0provider。 |
| lineage实现 | 40项定向通过/15.253秒，包括正式MCP保存/inspect/报告、分支/回滚、hash失效、反向修改、长ID/链与可写失败；不是全量测试。 |
| lineage独审 | 发现frame-only变更未计corrections却误报完整的缺口；原正式MCP反例1失败/9.33秒，修后原断言1通过/6.38秒；原失败保留，现无剩余阻断。 |
| 历史事实核对 | C02→C03确为15操作/13显式对象，默认1611 UTF-8 bytes；独审14项逐值/hash核对通过，原历史读入文件不变。 |

参数谱系源码 `de62458f`+`b7da2ee4`（隔离原提交e62b8fd1+222210ee）沿明确父链核source/report/proposal与精确参数绑定，不按最大候选猜当前目标。generic保存默认route和显式inspect各返回一份≤2048 UTF-8字节摘要；全历史含原before/after/reason/source_refs，按内容hash放独立tool_reports，可通过已有reader的/steps及后续引用读取。历史计数不是净差分或必须恢复清单。父子mesh_frame变化未被correction审计覆盖时标partial/history_complete=false并保留限制，不将未知sidecar当可信操作记录。任何提示错误不阻断原保存，也不授权patch旧稿或自动合并。

外部独审：`analysis/SECTION-PREVIEW-INDEPENDENT-REVIEW.md`、`LINEAGE-INDEPENDENT-REVIEW.md`；完整实施与旧轨迹重放见`LINEAGE-IMPLEMENTATION.md`。登记前副本及`domain146-registration-verification.json`确认42条runtime、145条旧domain、aliases及metadata不变，仅新增domain146。新真实结果如下，不将指导性协议成功等同整案质量或提速。

## domain146真实窄验收与分工全链开销

所有新运行冻结897d7610/runtime42/domain146，既有订阅GPT-6.1 Sol/high，0供应商重试/0格式修补，原run保持。各组显式限制是实验协议，不是产品档位。

| 新协议/腿 | 真实请求/工具 | controller秒 | 全树reported tokens | 结果 |
|---|---:|---:|---:|---|
| 固定ROI父＋子＋强制完整回读 | 5/3，父4子1 | 62.8534 | 21,037（父16,721＋子4,316） | 33协议项及独立视觉/账务通过 |
| 同ROI单体 | 2/1 | 21.7293 | 2,330 | 21协议项及独立视觉/账务通过 |
| 谱系v1，格式文档被窄工具集屏蔽 | 6/5 | 43.9726 | 19,422 | 历史理解正确，唯一局修失败；仅部分证据，不是验收通过 |
| 谱系v2，正式格式文档可达 | 5/6 | 33.8716 | 30,141 | 50协议项与独立源/语义/账务核对通过 |

父子对照先B后A，队列89.1693秒包含各腿和臂外审计；另预制seed2.547915秒，不重复计入每腿。实际parent/child都收到同258×245历史固定ROI，来源hash/native交接、逐ID采纳及原文均核验。两臂都正确识别Bicycle、只引用BRICK片段并对完整文字保留unknown。没有错误子答，因此**没有证明父级纠错能力**，也没测自主选ROI或决定是否派工。原435文件前后不变，终态seal749a57ee…再次通过。证据：`analysis/DELEGATION-PARENT-INDEPENDENT-REVIEW.md`、`delegation-parent-experiment/results/RESULTS.md`。

本题委派约单体2.89倍端到端/9.03倍tokens，不能外推最优分工成本。逐值核对发现delegate默认shown.results[0]已完整包含后续read_mesh_worker返回的全部claim、uncertainty、evidence_sources和canonical hash；该协议**强制的完整回读是重复工作**。父级自身16,721tokens已超过直接作答2,330，单换便宜worker不能消除现有父级开销。机械删旧read call/output仅少5704字节，未实际执行新策略，不能当节省tokens/秒。delegate16.5007秒内已包含child8.6770秒，不再相加。

后续分工探索优先：已有完整delegate结果直接核对；若缺项或要量测/图片再按selector读；若干独立ROI一批处理以分摊派工；另做预冻结的错误子答注入评父级拒错。最后再在同合同下比较弱模型。当前只是外部探索方案，不改生产异构/异步调度；没有新Qwen调用。见`analysis/DELEGATION-NEXT-EXPERIMENTS.md`及`REDUNDANT-READ-ADDENDUM.json`，不是新的模型排名或费用报价。

### 谱系窄协议：失败归因、文档可达性与源保留

v1正式工具先准备C01参数稿、C02南窗局修，2工具/4.545095秒，来源是明确合成声明。真实模型正确读出祖先C01、1条历史操作与南窗对象，但唯一revise把z放顶层，工具拒绝`unknown fields ['z']`；北门未改、没有C03，模型如实报告saved=null并正式回读未改值。冻结外部audit又因将`Tool error (isError=true):`前缀当纯JSON而崩溃，原错误和脚本保留，追加独审仅判partial_evidence_not_acceptance_pass。332原文件不变，seal db60a0…终态通过。

独审证实该窄runner替换system并只开放3个域工具，revise说明指向的get_bim_reference被移除；operations_json仅string类型，历史before/after记录也不是请求格式。正式产品通过get_bim_reference('edits')提供正确changes示例，因此**不能把这个外部合同缺口当生产harness或Sol泛化失败**，没有因此修改产品。见`analysis/LINEAGE-REAL-INDEPENDENT-REVIEW.md`与`lineage-acceptance/ORIGINAL-AUDIT-FAILURE.txt`。

v2另建目录/新封印90aef420…，恢复正式get_bim_reference且只允许edits，写前必须读取实际文档，8请求/10工具/300秒、一次修改，无重试。只复制最初39份PRE-model seed文件、逐项核hash，无v1模型答案；新复制核验0.069291秒，旧2setup/4.545095秒作为共享历史准备，未再次发生。独审还修正了外部seed路径和新增未登记文件漏验；20项纯检查成功覆盖、唯一正式MCP/native smoke1通过/10.08秒、55检查真、0provider；重复守卫复验不相加为唯一覆盖数。

真实v2第二响应合批读取历史、正式edits和当前开口；文档完整原文进入产生唯一revise的native请求，随后正确使用changes.z保存C03。最终北门0..2.3m，南窗1.2..2.2m及东门0..2.1m保持；除所需北门Z/来源、追加correction及派生hash外，源逐值不变。独立核5请求/6工具/30,141tokens，384原run文件不变，38不可变seed文件相同，仅运行日志正常追加且旧前缀完整。50项冻结协议审计与另38项独审通过（不相加为唯一覆盖数），末次seal验证后才解除源码冻结。见`analysis/LINEAGE-V2-REAL-INDEPENDENT-REVIEW.md`（SHA256 46d98de4436e2a11994aedff8fc78ccf2c71285aa0801e03f54eac89bb2755bf）。两版合同/预算不同，33.87与43.97秒不能作同条件速度收益，也不证明模型会自主选择正确编辑路线。

### 已知平面简化与后续边界

只读追查Lee002的23F前室/H.R./hall：原全图、2倍局部图、无网格放大图及C01回叠均进入native；左梯前室在首稿已明确主动整合，H.R.遗漏原因无法从轨迹确定，hall抽象也自首稿存在。三轮关系检查重复同3组样本，未覆盖这些对象；工具已明示caller_selected_point_pairs_only与drawing_fidelity=not_evaluated，不能把局部通过当整层正确，也未发现反馈隐藏。

当前partial_inference已保护真实隔墙/空间/柜位，不能据单案直接叠加新门或加长guidance。此问题与用户认可C01、明日讨论的可接受简化范围有关，今晚不替用户确定档位。仅保留针对拟简化分隔选关系样本的后续窄探索；原644份证据hash不变，0新工作模型。见`analysis/KNOWN-PLAN-SEMANTIC-SCOPE-DIAGNOSIS.md`。

本次lineage隔离树已核4个源/测试文件与main相同、tracked/untracked干净、仅缓存且无活动进程后移除，branch222210ee及外部证据保持；清理记录`analysis/lineage-worktree-cleanup.json`。其他会话树和`.playwright-cli/`不动、桌面未更新。09:00早报前如继续探索，先核外部handoff和实际活动进程，禁止复跑旧目录或把已解释的失败当待自动修复。

## 利园001完整验收与独立质量复核已结束

冻结 `dd8768f3`、runtime42/domain141，原三个盲输入和原scope保持；48模型/240工具、无额外墙钟或token上限、worker8、Sol/high及32k本地输出预留。旧候选、GT、评价不进入模型。实际输出 `D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-overnight-20261024-001`。

原生 runtime 用时 **1339.250秒（22分19秒）**，外部准备至退出1361.086秒。主控24请求、worker3请求，全27次实际请求已核Sol/high；62工具（60+2），全树3,050,309实报tokens。C04显式finish，finalization.delivery、7个绑定交付文件hash及只读前后不变检查通过。首机器有效C03工具返回11:27.22，之后10:52.03；末稿C04返回19:02.91，至终态3:16.34。文件mtime、build_timings和工具返回分别记录，不混成一个首稿时间。

独立Codex视觉审阅支持当前Lite BIM用途的主体外形、主要窗带和核心组织；318开口无宿主问题，7连续核心无中间foreign盖板，23占用层×3lift/2stair的115条接入具备潜在连通。AIR/PIPE无门不扣分。上部复杂窗型、屋顶设备和非23F内部仍有明示简化/推断，不称测绘或竣工还原。32条floor记录包含7核心支撑，并非32物理层；public_names序号让F23图标题显示F30，仅展示混淆，未改源身份。

固定2304射线中四案共同有效1122：新稿5/10/30cm内34.76%/81.37%/95.01%，中位误差5.94cm。两两新/前稿共同1155中，30cm为94.11%对94.46%，不能挑分母说全面改善。详细数值、覆盖、独立视觉及边界分别在外部 `evaluation/lee/NUMERIC-AND-MECHANISM-REVIEW.md`、`INDEPENDENT-VISUAL-REVIEW.md`。

与前轮runtime41/domain138的27分37.97秒相比，本样本少318.72秒（5分18.72秒），实际请求33→27；仍比旧runtime36/domain132的20分40.70秒慢98.55秒。代码、模型自主轨迹与服务时点都不同，不将单次差值归为某个特性的严格因果收益。本次完整验收**尚不含**下面的新证据投影改动。

封印及终态审计：外部 `lee-acceptance/`；固定原frame的9视角射线/宿主/核心检查与独立视觉在 `evaluation/lee/`。模型申报质量、机器合法性及独立质量分别记录。方法中的 `binding.is_delivered_candidate` 只是从completion取候选；本轮正式交付依据另有terminal-audit，不将该描述字段当验收门。

字段聚合、failed_batch引用局修、image_view_ids预取和worker量测摘要四项在本轮均未触发，不能把用时改善归因于它们。declared-host提示已出现且随后补丁成功，但不单独作因果断言。C01/C02唯一severe是floor_coverage毫米级缺口；进一步确定性重放发现**原始parametric_plan完整覆盖，0.1m supporting-line规整后产生真实裂口**。源验证在正确拒绝规整结果，不放宽阈值。后续已在domain144合入可定位诊断，详见下文；没有自动拓扑修复。

## 已集成的新改动

源码提交 `772aa6ca64c1dfad80d2294c87fd3f87415dc92b`，runtime42/domain142；仅mesh worker主控/读图端domain变动。利园终态封印核验后才合入主库。没有改变几何合法性、档位或停止条件。

1. 每个worker答案内，完整相同的图片证据对象只展示一份，逐claim用evidence_ref引用。按整个对象hash去重，原图、ROI、来源快照不同不合并；原始outcome/journal完整保留。
2. read_mesh_worker指定图片或量测selector时，只展示所选材料、完整量测和结果hash/完整答案读取入口，不重复整个答案与无关目录。无selector仍可读取完整答案及唯一证据表，不依赖隐式“已经读过”状态。worker结束不等于质量认可，快照适用性与fidelity边界保持。
3. 辅助裁图坐标说明消除冲突：题目不需要或禁止坐标时允许省略/null；任何实际填写的框仍须采用原图坐标并位于实际供图范围。schema及坐标校验不放松。

| 验证范围 | 结果 |
|---|---|
| 新回执、原projection及ROI定向 | 32通过，23.18秒 |
| worker/evidence相邻 | 88通过，28.82秒 |
| 独立审阅，含4个新反例 | 36通过，26.10秒，无剩余阻断；与上述有重叠，不相加为唯一覆盖数 |
| 旧回执纯内存重放 | Lee取图read 46,831→3,317字符；Energy delegate 28,247→14,190；ROI Sol预取10,681→5,617。原raw/PNG、逐claim证据和结果hash不变，Lee55个有效查询保留 |

独审位于 `D:/Sim-BIM-Agent/cache/mesh-harness-overnight-20261024/receipts-review/`；受审主文件SHA256为 `3854d1355efc1b3bbf0a64cd28341153f706d5391d979684ca02af9c8c3c3b10`。字符减少不等于provider token、费用或整案秒数收益。

## 新版真实Sol小验收

- `receipt-acceptance/`：冻结54d1255e/runtime42/domain142，真实Sol主控+worker于85.589秒完成（另共享准备1.214秒），6真实请求/4工具、全树39,979 tokens；其中parent23,952、worker16,027，controller.reported_tokens仅父级。33协议检查及16独审证据检查全真，两条worker自然语言与实际valid flags/3.1899948120124404m一致。显式委派、同次取图和完整量测、再取完整答案的guided流程成功，不证明自主调度。原416文件读前后hash不变。离线5通过/14.56秒、38断言与第一次解析失败历史另存。
- `delegation-experiment/`：同版两题四腿全部终态，123.81秒、6真实Sol/high请求、28,030实报tokens，另14 synthetic parent请求与12正式工具。四腿均19协议项及任务合同通过；实际图片字节、native新坐标提示、max_answer_repairs=0外部同臂控制均验证。末腿后显式原seal核验两次通过。独立视觉与提示审阅已通过，见 `evaluation/facade/`；合同与视觉两种证据分别保留。旧版两次离线smoke保留，新版离线1通过/40.93秒、57检查全真，0真实provider。

| 题目/供图 | 子任务真实请求 | 实报tokens | worker秒 | 每腿完整秒（含准备/审计） |
|---|---:|---:|---:|---:|
| 招牌/预取 | 1 | 4,712 | 17.99 | 32.60 |
| 招牌/按需 | 2 | 9,769 | 22.16 | 32.78 |
| 窗带/按需 | 2 | 9,260 | 16.54 | 28.03 |
| 窗带/预取 | 1 | 4,289 | 13.93 | 27.77 |

预取少一次请求且输入用量下降，**本样本端到端几乎持平**；不同题目、单次样本与服务时点不能推稳定提速。订阅费用未知。父级是脚本、核验由Codex在外部进行，未计开发/协调推理费用，不能声称完整分工已经降本。

实验输入与参考分别冻结，only task PNG和必要原GLB进入worker。正式schema/来源、任务字段合同、视觉事实可用性三层分别评价。已有图和固定ROI由评审侧预先给定，不能称为自主寻找视图。

## 历史归因更正及成本边界

原ROI四腿全部7次真实请求的高优先级说明要求“cite ... and box_pixels”，与题面禁坐标/notes要求null冲突。Qwen off输出不满足题目合同是事实，但该样本不能作为纯模型遵从能力失败的证据；其他三腿没输出坐标也不证明提示没有影响。原run全文件hash保持，追加审计见 `delegation-design/prompt-audit/`，不改旧答案。

`delegation-design`早期方案写“人工参考/人工核验”不准确，实际由独立Codex子agent建立视觉参考，主控另看图核对，没有用户人工标注。原盲参考数值和封印保持；`reference-provenance-correction.json`（v1，SHA256 `d2f32042d01d1d9ae049090719732851c055c09baac8daba571370673c6fc7a8`）追加更正，新run seal必须纳入。Codex开发/审阅/协调推理开销未测，记unknown，不能填0或声称已计完整主控/人工成本。真实worker、脚本parent、准备和独立核验的费用边界分别说明。

## 直接量测回执已集成并通过真实窄验收

量测源码 `af7186e6`（隔离原提交 `e0db9299`）已合入，登记 **runtime42/domain143**。完整数字和三角面证据仍留在 raw/journal 与内容寻址报告，模型默认收到保留全部数值、方向、有效性及 caveat 的摘要；需要细部时使用已有 report JSON pointer。缺少历史 PNG 不妨碍已有数值读取，缺图明确标注；子任务完整报告仍限定自身目录。没有修改量测算法、候选、worker canonical 或停止条件。

86项唯一实现定向检查通过；独立 actual-MCP/native smoke 5通过/6.14秒、32检查全真，另报告目录写失败反例1通过/6.16秒，无阻断。不是真实模型结果。独审与源码hash见外部 `direct-measurement-acceptance/INDEPENDENT-PRODUCT-REVIEW.md`。

六条旧回执用正式 hook 反事实重放：shown字符125,103→49,082，其中也包含JSON紧凑排版收益；统一排版后净减少41.21%，按原请求出现次数加权少335,238字符。六条完整报告/分页/pointer均等值，原证据不变；补的snapshot从终态原文件重建，不能倒称历史调用时已验真。不是token/费用/耗时实测。见外部 `analysis/FORMAL-HISTORY-MEASUREMENT-PROJECTION.md`。

冻结 `c543c7a7`/domain143 的普通单parent Sol/high窄验收已完成：37.555865秒（含启动8.723814秒，另准备1.162072秒），3真实请求/2工具/6,563实报tokens。6请求/8工具/300秒上限内，一次量测后按实际报告入口读取 `/queries/0/triangle_surface_evidence`，没有worker、重试或格式修补。27协议项与17独审项全真；最终3.1899948120124404m等于两个实际XYZ的距离，三角面13字段逐值一致，报告/子树hash区分正确，161原run文件hash未变。原seal `7dfebca269e77243de3cd7d6ec1720997d1457d5ee3e7381c04a1e2974771584`及输入完整性已通过，源码冻结解除。证据在外部 `direct-measurement-acceptance/REAL-SEMANTIC-REVIEW.md`；这证明指导性数值协议可用，不是整楼质量、自主调度或用时降幅。其后规整定位器已完成独审并另登记domain144。

## 规整共享角点诊断已集成并实际触发

最终源码 `1cc8ce00`（隔离受审 `1e6c14d3`）登记 **runtime42/domain144**。原支持线独立量化仍保持；新增定位在写 adopted/candidate 之前，检查同模板实质共享角点附近是否出现 source 原数值谓词同样拒绝的缺失/越界/重叠，聚合坐标、frame、声明路径与完整报告入口。共线冗余点和现有数值容差内的薄楔不因此拒绝。已有原模板平面谓词需先通过，避免把原始输入缺口归因规整。

该定位限于可由局部模板证明的直棱柱情形：含vertical_sections或任何实际实例显式spanning_space_ids的模板记not_assessed，仍走原完整source/topology门；未用模板不触发。通用source入口保留原跨层身份并向内部定位器传递组成上下文。没有新公开规整策略、自动焊接/填缝、放宽阈值或默认移动锚点。

最终实现150项通过/13.05秒；独立新增17项及3反例共20通过/3.12秒（有重叠，不相加）。旧C01定位4处、C02定位1处，C03完整adopted与历史逐值一致；正式MCP失败前无candidate/adopted副作用，原seed/draft保持，完整audit可通过已有report读回。默认摘要C01为6446B、C02为2445B。独审无阻断，证据在外部 `analysis/REGULARIZATION-INDEPENDENT-REVIEW.md` 和 `regularization-junction-cause.md`。尚未证明模型据此修复更快。

集成只在test_groups新增条目处发生内容冲突，已同时保留两个新模块。期间误对尚未完成的cherry-pick做了一次未发布登记；未用其启动任何模型、未提交/推送，原registry已按登记前副本逐字恢复，再以最终受审源码重新登记。错误尝试仅保留外部审计副本；最终42条runtime、143条历史domain及aliases均逐值不变，新domain144核验通过。

原输入Lee002已于最终HEAD `0da485d6`/domain144新封印运行并终态；原三个输入、48模型/240工具、无额外墙钟/token上限、worker8和原scope保持。封印 `897bb970927a832bad064d470bb80546e40f292c908fce503d2ca7cfaf1d39f0`；原产物、外部方法和源码在完成独审后再次核验。三空间合成修复仅保留备用设计，没有为增加次数启动。

## Lee002：整案时间回涨与修订价值

输出 `D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-overnight-20261024-002`。C06显式finish、completion与7个绑定交付文件hash通过；原生24:48.55，controller含准备25:17.88。34真实请求（主控30/worker4）、74工具（69/5），全树4,602,954 tokens=主控4,522,945+worker80,009，由原生usage逐请求复核，无缺失。外部审计中 `usage_accounting` 是主控、`root_usage_accounting` 是树级，不能按字段名猜。全run4669文件前后hash相同。

| 原生同口径 | 001 | 002 | 变化 |
|---|---:|---:|---:|
| 总用时 | 22:19.25 | 24:48.55 | +2:29.30 |
| 主控模型秒 | 1128.996 | 1140.083 | +11.087 |
| 主控等待worker秒 | 46.470 | 136.512 | +90.042 |
| 其余主控工具秒 | 98.210 | 123.846 | +25.636 |
| 其他运行开销秒 | 65.574 | 88.108 | +22.533 |
| 首机器有效稿返回 | 11:27.22 | 11:57.16 | +29.94秒 |
| 首有效至终态 | 10:52.03 | 12:51.39 | +1:59.36 |
| 末稿返回至终态 | 3:16.34 | 1:29.61 | -1:46.74（未舍入原值差） |

不是末次finish检查再次拉长；本轮末稿尾段反而缩短。worker等待已经计入总墙钟，不能二次相加。没有provider失败/自动重试/退避，但同一时段有一次独立boundary小请求重叠，且不同运行的自主策略和服务时点不同；不能将分项差值解释为某个代码改动的严格因果。002仍比旧20:40.70慢4:07.85，比前次27:37.97快2:49.42，没有稳定趋势。

进一步核对worker合同：001自选立面后未做量测，最终明确标高/节距/host跨度未解决；002实际量测101个有效点，输出更完整，主控明确采用3.2m节距/13.2m带宽及18+3例外，没有照抄推断起点。两轮初始是不同概览图，均非固定ROI小题；002额外视图是不同取景，不是相同PNG重取。+90.042秒中worker模型汇总增加83.985秒、工具仅1.765秒，不能归为渲染慢、provider排队或全部无效等待。外部 `analysis/WORKER-WAIT-ATTRIBUTION.md` 保留合同、逐轮及采用证据；开放式选视角/选测点的调查成本与固定ROI一次答复必须分开。

首稿前六次旧ring拒绝（10→6→4→4→2→4项），工具内核共0.785秒，跨调用126.010秒。之后新增shared_junctions实际返回office/retail两处；下一补丁成功产出C01，间隔74.294秒，包含模型间隔53.481秒和构建20.813秒。补丁共9项，改frame/外壳/开口/入口，不能归为仅修两个角点或证明新增定位节省了整段首稿时间。字段聚合、failed_batch重试、worker image_view_ids仍未触发。

C01之后：C02缩屋顶PH并修底商窗；C03修最高三层窗区和6个内幕墙组、撤销无充分证据的灰块窗；模型明确选回C02参数基线改底商，C04因connection内部缺source_refs无效，补该字段成C05；C06恢复C03已做的13项开口几何/身份编辑。C03回执及真实native已明示exact_plan=false、patch_allowed=false、推荐revise_bim；**不是harness静默回退**。自动合并有几何风险，可先探索旧基线选择时有界提示间隔编辑；当前没有实施。

三次主控量测实际采用新摘要：raw→shown字符12,779→5,001、23,485→8,159、11,678→5,584，数值/坐标/valid/来源hash及完整报告逐值核对通过；没有量测report重读。另8次剖面segments分页共35,140 UTF-8字节、来自3个响应批次，后5次同批，不能说5轮往返；底商补丁实际引用segments22..35。分页仍有改进空间，但不能一概记为无效检查或归咎新量测摘要。

| 固定2304射线的比较 | 共同有效点 | 5/10/30cm内 | 中位绝对深度误差 |
|---|---:|---|---|
| 002 C01→C06 | 1179 | 19.34/67.26/91.43% → 21.63/69.72/93.72% | 7.01→6.88cm |
| 001 C04→002 C06 | 1106 | 34.90/79.93/93.85% → 22.06/68.81/93.49% | 5.96→6.81cm |

各自有效覆盖、no-hit/edge和逐视角保留，不混分母。002同run末稿有有限外壳改善，独立视觉也确认顶部窗组、底商退进和PH范围更贴原输入；但相较001紧分箱与中位退步，平均误差微好，不是全指标单向变化。深度残差也不评价窗的材质或室内空间身份。

已知23F左梯前室并入连续楼梯体，H.R.柜位不单列，可见分隔未完整保留；首有效C01即有，C06未修复。147空间ID/用途/高度及232连接记录不变，69个电梯门端点约8.06cm变动不等于前室关系修复。309开口宿主诊断无问题，115层级核心联系潜在连通、7连续体未见所检水平封堵；门unknown不等于现场通行。32条floor含7核心容器，不是32层。独立Codex支持轻量外形/主要核心位置用途，不能替用户接受已知图纸身份合并或称全部核心还原。

完整报告：外部 `evaluation/lee-next/NUMERIC-AND-MECHANISM-REVIEW.md`、`INDEPENDENT-VISUAL-REVIEW.md`；精确时间/用量/来源见 `summary-deltas.json`、`usage-native-crosscheck.json`、`lineage-field-evidence.json`。末次封印与方法验证在 `post-evaluation-verification.json`。质量审阅没有发新工作模型请求或改原候选。

## 分工自然遮挡边界：模型回答可用，外部审计曾假失败

四腿独立语义已复核通过：两次识别Miele/BRICK LANE/Bicycle，两次均为6条完整可见窗带，无越界推断；6个真实native developer提示均已消歧、图像字节一致，原1223文件hash不变。见 `evaluation/facade/INDEPENDENT-VISUAL-AND-PROMPT-REVIEW.md`。

新增 `delegation-boundary/` 已终态：原图自然树叶遮挡、258×245固定裁图，PNG SHA256 `9686947d9c27d4c986e2492cf7764c0c046b1a64d871749fad7cdfba8f42f877`。1次真实Sol/high、4357tokens、0子工具/格式修补/重试；另4次脚本父请求/3父工具。worker14.639秒、leg25.347秒、controller28.968秒，时间嵌套不相加。回答看见Bicycle、文字完整性unknown，只引用BRICK可读片段，没有补全被挡名称；root另看图复核支持这个有界结论。输入不含参考答案，独立Codex参考不冒称用户GT。

原controller确为stopped、原协议27/28及任务合同9/9；失败项是外部auditor将解码原句搜索于json.dumps输出，句内引号转义造成假阴性。原状态/审计/封印没有改写，**没有重跑模型**。追加17项独审逐层证明native answer→hydrated→formal raw/shown→下次脚本父请求内容一致，275原run文件及19受保护文件保持。纠正位于 `delegation-boundary/correction/READBACK-CORRECTION-v1.md`、`readback-correction-v1.json`；root图像与回读核对在 `ROOT-INDEPENDENT-READBACK-AND-VISUAL.json`。原seal `ff854ae346b8c109831c4ddf06d8f7c24d70481f092f6476636eca513ad62f0e` 终态后再次核验。

此小题证明既定ROI上的一次Sol回答能保留unknown，不证明普遍校准、弱模型能力或自主裁图/采纳。分工优先候选仍是固定ROI短文字/清晰图标，再考虑无遮挡完整窗带计数；数值搬运/校验优先程序处理。ROI选择、空间关系、同墙/跨层推断与最终几何采用留主控。后续比较弱模型要同PNG/同合同，并计主控核验与失败成本；本夜没有新增任何弱模型测试。

## domain145：原声明source_refs反馈已合入，真实窄验收完成

实现 `1922c9d9`（隔离 `0099cc85`）仅改既有字段诊断器及其测试，已登记runtime42/domain145。global connections使用正式Source/WallOpening的字段类型、非空与coercion规则；实际使用模板的compact spaces/doors/opening_rows/window_rows和core_connections复用编译器自身strings合同，实例/patch来源不代填本行缺失。未使用/被完整替代模板跳过；合法空window_rows.spans不误诊未被求值的source_refs值。正式失败时才并列附field_diagnostics，原几何/源判据及失败目录保留方式不变。

96项定向检查通过/12.52秒；独审复用该结果、另核实际C04/C05声明与正式source parser：C04精确定位原 `/connections/2/source_refs`，原展开错误仍在；C05诊断空且通过，原文件hash保持。实际Toolkit C04离线重放1.68秒，原失败加新pointer，未生成有效source。没有默认填值、自动改源或声称消灭第一次无效保存。证据在外部 `analysis/SOURCE-REF-PREFLIGHT-IMPLEMENTATION.md`、`SOURCE-REF-INDEPENDENT-REVIEW.md`。

`source-ref-acceptance/` 用合成单房间声明的3个缺失refs验证聚合定位→只补对应来源→正式全量回读，最多6次Sol请求/8工具/300秒，无worker/重试/格式修补。来源是实际保留的合成声明文件+pointer，不是建筑GT。开发树正式MCP四场景及native脚本闭环已离线通过、0provider；登记后的main无waiver smoke为5通过/9.61秒、36检查全真。root独立审脚本后冻结HEAD `64a303e5`，seal `429f4eef7871841cd095aa258745d4187b75d02aa1ccf35d4e5eb40ddbc665cc`。

真实输出 `D:/Sim-BIM-Agent/runs/mesh-source-ref-sol-acceptance-20261024-001`：controller43.381295秒，内含启动8.066113秒；receipt42.175900秒，另准备0.012764秒。4次Sol/high请求/3正式工具，21,942 tokens=20,992 input+950 output。模型使用实际failure的3个原pointer，只add对应来源，然后完整读回；原bad draft、所有其他计划字段、保存/读取hash和实际native回执匹配。不是自主发现来源或整楼提速验收。

原外部审计30/31、all_protocol_checks_pass=false保持：最终readback_source_refs还列出一个原有且正确的空间引用，原审计却要求该字典恰好等于3个新增目标。独审核对实际每轮GUIDE：exactly仅约束7个顶层字段，没有禁止嵌套对象列出其他真实读回值；实际patch与diagnostic_pointers仍严格3项。追加42项独立语义检查及反例全真，不与原31相加。四项引用均与原输入/保存plan/实际source declaration一致，无模型违约、无重跑、无改写得分。265原run文件及旧脚本/封印/审计字节保持，末次seal校验通过。证据在 `source-ref-acceptance/REAL-SEMANTIC-REVIEW.md` 及同名JSON。

参数基线与通用修订的有界衔接提示仅设计，见外部 `analysis/parameter-lineage-advisory-design.md`：基于明确父链与hash展示历史操作摘要，不能称净差分或必须恢复清单；保留用户主动回滚/分支选择，不自动合并几何、不按最大候选猜当前目标。档位/停止政策、生产异构/异步调度未实施；未更新桌面，`.playwright-cli/`原有资料保留。下一恢复先核外部handoff及实际状态，不重跑旧目录或把未完成项写成验收通过。

先回收两棵已集成的measurement/regularization短期工作树：核对全部源/测试与main字节一致、工作区干净，仅含160/145个缓存文件后移除；原分支和提交保留，正式证据/run未删，其他会话树保持。清理审计见外部 `analysis/completed-worktree-cleanup-*.json`。该检查点C余37.23GiB、D余101.73GiB。

其后source-ref树也已独审、集成、完成真实验收；确认与main两文件hash相同、工作区干净、仅145缓存文件、无活动运行进程后移除，分支0099cc85保留。其他会话树未动。最终清理审计 `analysis/source-ref-worktree-cleanup-*.json`；C余37.22GiB、D余101.74GiB。

## 测评合同补强与晨报恢复

针对本夜两项外部审计误判，新增独立 `delegation-audit-next/` 供未来实验复用：按ID比较解码后的statement，避免JSON引号转义子串误判；refs默认严格键集合，仅预先显式allow_verified_extras时允许逐pointer核实的真实额外项。RFC6901指针、重复ID/JSON键、错值/缺项/伪造pointer等33个定向反例通过/0.13秒，root源码审阅无阻断、0provider。这只是未来可选helper，未接入产品或旧auditor，未回写历史判定。调用方仍负责来源hash、非空要求和语义质量，需在看到未来答案前冻结合同。

以上段落是domain145前批检查点；参数/源修订衔接与小剖面已在后续domain146实施，最新真实结果和下一步以本文开头及外部handoff为准。09:00早报由本线程automation继续，不因已解释的原审计false状态重复测试；生产分工及档位留待讨论，没有新证据缺口时不为耗满窗口扩测。
