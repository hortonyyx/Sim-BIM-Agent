# 利園六期：单体办公混合输入与首测准备

2026-10-19。用户将首轮范围收窄到单体办公、100m以内、不过度复杂，尽量有同栋标准层平面。本轮选定 **Lee Garden Six／利園六期，香港铜锣湾礼顿道111号**，取得完整原贴图模型与明确标注23/F的全层平面，补齐材质兼容和混合输入预检。公共 Runtime、源BIM、kernel、工具及交付继续共用，mesh方法和验收独立推进。已准备到第一次完整实测之前，**0真实模型调用、未发布桌面**。

## 选定建筑与依据

- [业主项目页](https://www.hysan.com.hk/portfolio/lee-garden-six/)确认办公和低层零售用途及地址；[CVU/Skyscraper Center](https://www.skyscrapercenter.com/building/lee-garden-six/24522)列建筑高度82.6m。这是一栋有小角度弯折的办公塔楼，名字中的Lee Gardens系列不表示本次选取多楼综合体。模型外包宽/深/高约32.012/26.211/83.396m；公开高度与模型极值的定义不同，不把0.796m差值当精度通过。
- [香港地政总署独立建筑3D模型](https://data.gov.hk/en-data/dataset/hk-landsd-openmap-3d-visualisation-map-individualised-models/resource/fc4537ef-314f-4baf-9e47-22c91e6f1170)原资产为 `B371851542601063A0`、图幅11-SW-15B。仅用HTTP Range取目标资源和目录，未下载1.05GB整包。原件是glTF＋BIN＋JPG＋RGBA PNG，封装GLB时只内嵌资源并增加本地原点平移根，662个面、UV、贴图和alpha全部保留，无裁剪、补面、删透明部件或重建。这是公开建筑可视化模型，不宣称密集摄影测量原始扫描；来源及使用条款留在外部获取记录。
- 地址点最近的另一个5.5m附属对象已排除。选中的全高塔楼经官方业主外观照片、原件窗带/转角/顶部、23F弯折轮廓/朝向以及正式图中可读的LEE GARDEN SIX招牌交叉确认，身份不是只靠最近坐标猜测。
- [美联工商铺同栋楼页](https://www.midlandici.com.hk/en/building/CAB-Causeway-Bay-Lee-Garden-Six/B000088775)提供23WF图片，图内明确23/F、约2897sqft可租面积，可见完整周界、核心服务带、3电梯、2楼梯、卫生间、管井及柱。原JPEG转PNG后解码RGB完全一致，无裁剪、重绘或去水印。**直接依据只到23/F**；未取得可验证的全楼标准层适用声明，不能把它泛化到2–23F。
- 该图没有尺寸线/比例尺，可租面积不能标定整个外轮廓；23F的mesh标高、其他层复用、实际租户现状仍未知。业主24层与数据库地上23层计数差异未消解，2.36m净高不等于层高。以上区别进入模型可见的来源文字，但独立审查几何分析、GT和旧答案不进入输入。

## 准备包和审查

正式案例：`D:/Sim-BIM-Agent/cases/lee-garden-six-mixed-20261019`。

| 模型可见文件 | 作用 | SHA-256 |
|---|---|---|
| `inputs/building.glb`，5,017,904 bytes | 完整原件封装、全楼外形/立面 | `6ed1c026d7dcab9c65cd8fa77c304bfb1b6fc55ca2ca0e7b727bc0504c455061` |
| `inputs/auxiliary/23f.png`，251,879 bytes | 明确23/F的局部平面依据 | `23c5335eaf309a4f7cf27d68b3021e6badeb64ab20ff7010688b710302188d8f` |
| `inputs/building.json`，3,651 bytes | 助手按用户授权代拟的中文任务说明、原用户范围、来源事实和不确定性 | `dfa1fc9f8af47bbeb348b6f8c0e6bd714093db38539a456b9bde6869fcde8f10` |

审计在独立的 `audit/`，含原图、来源、转换记录、正式九视图与像素缓冲、独立代码/原件/native审查、冻结清单、普通运行配置和实际预检结果。仅上表3个文件进入模型输入；观察图、人工比对与独审记录不预先作为模型答案提供。

正式九视图包括四方位、四斜视和顶视，全部原662面，无bounds过滤。独审接受主体完整性、主要窗带可读性和屋顶/基座；暗核心与部分背面细节弱，不能宣称逐窗或所有细部均可精确辨识。原件4资源＋GLB前后哈希相同，9组创建时PNG/NPZ/metadata绑定通过；24次半透明查询均明确无XYZ且非背景，27次不透明查询均有效。跨视图608个无效像素出现次数不是608个独立表面点。

实际 `check_mesh_readiness.py audit/readiness.json` 返回 `ready_for_separate_launch_approval`，`no_model_calls=true`、`live_launch_authorized=false`，固定domain121/runtime31。预检只确认冻结输入、普通配置及版本，不自动认证建筑身份、图纸适用范围或BIM质量；后者由明确的审查记录承担。运行输出名保留为 `D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-first-001`，本轮未创建。

## 本轮实现与缺陷修复

1. 原观察器遇到屋顶24个BLEND面会拒绝整栋。现在保留原RGBA与alpha factor，逐像素按深度进行线性RGB合成，alpha零不遮挡；半开三角边避免同一四边形对角线重复透明叠加。真正参与可见合成的分数alpha使该像素不可量测，返回 `transparent_composite`，不当背景或零残差。前方不透明面完全遮住的半透明层不会错误使其无效。无效掩码与创建缓冲哈希绑定，缺失/篡改拒绝；位置比较明确排除这些样本。原三角面截线仍是载体面，不变成纹理孔隙边界。片元/层数预算超限明确拒绝视图，无部分成功。
2. 开发预检schema2接纳恰好一个原GLB、辅助PNG和至多一个来源声明JSON，逐项冻结角色、哈希、来源与适用范围，复用普通配置加载与argv。schema1仍为旧GLB-only语义。
3. 独审发现P2：普通配置加载会先读错误input_root下的图/JSON，再发现与冻结根不符。修复为先检查根路径、角色、白名单和审计隔离，之后才解码输入。原反例复核外部读取0次。没有放松原件身份、篡改或路径限制。

工具说明已补充透明可见但不可量测的语义。公共runtime实现保持；MASK、完整PBR光照/折射、单空间孔洞、连续倾斜/变截面、跨多个曲线弦的单开口仍未支持，非正交最小净宽仍未评。既有公共覆盖、重叠和宿主检查保持。

## 离线验证与边界

最终开发版本 **domain-v121-20261019 / runtime-v31-20261019**，domain451文件、runtime46文件。最终入口用真实本地MCP，合成透明夹具和脚本响应，串通曲线模板、原观察、worker截线/比较、回读、回叠、finish及同配置恢复；父6＋子3共9脚本响应、8工具，交付有效且恢复无新增请求。实际64比较样本含20半透明、44未命中，20项均不进入signed/absolute残差；该入口夹具没有有效残差样本，正向对应合同由之前单元检查覆盖。`fidelity=not_evaluated`，这不是工作模型自主建模。

| 范围 | 结果 | 耗时、证据及调用 |
|---|---|---|
| 混合输入/预检首次2模块 | 46通过、1失败 | 9.303s；`cache/pytest/mesh-office-mixed-preflight/0311e78aea80/results.xml`；新测试错写复数CLI参数，产品实际是单数；修正后复验 |
| 参数断言修复后预检 | 30通过 | 6.204s；`mesh-office-mixed-preflight-recheck/87c9245faa7b/results.xml` |
| BLEND观察＋比较最终 | 58通过 | 3.982s；`mesh-blend-final-20261019/50fd80ad15a8/results.xml` |
| 同范围独立复核 | 58通过，重复范围 | 3.237s；`mesh-blend-independent-20261019/82230bfe8fd2/results.xml`；另确认NPZ掩码篡改拒绝 |
| 读取顺序P2修复后预检 | 34通过 | 6.132s；`mesh-office-preflight-order-fix/249f322bcdf8/results.xml` |
| 独立预检复核 | 34通过，重复范围 | 6.081s；`mesh-readiness-independent-orderfix-20261019/cee67a74adad/results.xml`；原反例外部读取0 |
| domain121正式入口完整链 | 1通过 | 29.902s；`lee-garden-mixed-native-entry-20261019/9a9196fe4932/results.xml`；9脚本响应/8工具，无真实模型 |
| 实际原件正式9图＋点查询 | 9图成功，51查询符合合同 | 渲染共4.103s，单图0.205–0.578s，加载0.188s；`audit/native/material_render_audit.json`；0模型 |
| 最终3输入案例预检 | ready | `audit/preflight-result.json`；0模型，不启动运行 |

表中缓存相对项均在 `D:/Sim-BIM-Agent/cache/pytest/`，不相加成唯一检查总数。检查不覆盖全历史集、真实模型行为、整栋速度、外窗识别或独立5/10/30cm精度；尚未生成本案例源BIM、平面回叠、BIM viewer或模型用量，均待首测。9图的渲染时间不能充当整案耗时。

## 下一次真实测试的现成入口

`audit/run-config.json` 使用普通single_model，`gpt-6-sol/high`，可选同模型只读worker最大并发8；父子共享48模型请求/240工具，每响应上限32000tokens，无整案时间或token硬截止。目标仍为5–10分钟，质量和实际速度分别评价。模型自行确定平面/mesh对应、重复层、例外与内部推断，审计中人工分析不输入。未来启动沿用普通 `sim_bim.py run <config> --case lee-garden-six-mixed`，本次未执行。

首次实测应单独报告：真实请求/工具/用量与耗时、完整交付及恢复、原mesh/平面/保存源模型的对应、有效残差覆盖与分档、外窗宿主和内部合理性、未解问题及人工介入。完成标记、模型自审与独立精度仍分开。

## 收尾

首版独立审查无剩余P1/P2；原件和正式native输入均获接受，代码与案例预检边界分开。首版独审再次执行预检通过，3输入/11审图PNG/33审计文件绑定有效，输出目录不存在；首版manifest哈希 `6fa9f33a21b07eaad0b257e2e8c61fa2a9081a12e180142f7dcbbbdbaa47c36f`。随后追加任务说明时，首版输入、manifest、preflight和独审报告原样归档到 `audit/revisions/before-user-brief-20261019/`，当前状态见下面补充。

版本验证及暂存区497个封印文件逐一真实字节核验全部通过，历史runtime/domain登记未改写；证据 `D:/Sim-BIM-Agent/cache/lee-garden-mixed-staged-seal-20261019.json`。本批只扩展已分类测试模块，无新增模块遗漏；`git diff --check`通过。Git保存程序、小报告及证据索引，外部模型/图片/缓存仍在本机D盘，未宣称远端备份。无新工作树，既有Go工作树未动；桌面仍runtime24/domain110，未发布。收尾磁盘剩余C 35.14 GiB、D 120.12 GiB。

## 同日补充：简单任务说明与后续解耦方向

用户要求助手先代拟简单文字输入，后续再设计供多研究线、多输入形式共用的说明方式。既有building_input入口已接收UTF-8 JSON对象；本次只在本案声明前置 `task_brief` 和 `task_brief_origin`，明确是助手根据本次用户授权代拟，原字段逐项保持。说明涵盖整栋轻量BIM、外形/楼层/外窗优先、23F适用范围、未知内部合理简化及有标注的量测/推断；无房间坐标、GT、预拆立面任务、硬性房间数或精度阈值。原mesh、PNG和运行配置字节保持。

当前manifest为 `88d744c92a1a6bf6abab5efe2e2c3b23b56f842f472cb3a040792c5f78b9104d`，增加 `input-brief-change.json` 绑定新旧声明，预检仍ready。程序未改、domain121/runtime31保持，仅做案例配置/哈希预检与说明审查，不重复pytest或原件渲染，真实模型0。新说明被保留在实际已配置的building.json内，未单独增加一个不会加载的文本附件。

| 补充验证 | 结果 | 范围与未覆盖 |
|---|---|---|
| 普通配置及实际readiness预检 | ready；3输入、11审图PNG、34审计文件绑定有效 | 无模型、无新BIM；沿用既有代码验证，不覆盖真实理解效果 |
| 原声明字段、mesh/PNG与运行配置 | 原字段全部相同，mesh/PNG/config哈希保持 | 仅前置助手代拟brief，不将其标成用户逐字确认或实测事实 |
| 独立说明审查及单次预检 | 通过，无额外硬性条件或答案注入，输出目录不存在 | `audit/final_integration_review.json`绑定当前manifest；0pytest、0真实模型，预检耗时未单独记录 |

用户确认长期方向为调度员按任务选择模型、角色和工具包；当前仅底层职责分开，mesh worker继承主模型/参数并使用固定只读角色/工具集合。调度员可拆问题、选证据和预算，尚不能逐任务动态改模型或工具包；专门分立面角色/产物合同留待后续设计。图纸线原有平面/立面角色及模型配置不等于mesh已完成同样分工。该方向已同步到模型与输入策略和当前交接，本轮不实施编排扩展。

补充收尾：只有案例文字/审计与项目文档变化，程序和版本未变；diff检查通过，无新增工作树。磁盘剩余C 32.89 GiB、D 120.12 GiB。
