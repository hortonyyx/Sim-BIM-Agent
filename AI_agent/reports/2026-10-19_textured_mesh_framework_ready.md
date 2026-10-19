# Textured mesh：首轮完整测试前的框架收敛

2026-10-19。用户要求继续推进到框架基本搭全、首轮完整真实测试之前；随后指定先做办公楼，住宅留待专项调整。公共 Runtime、源 BIM、kernel、工具与交付继续共用，mesh 输入方法、观察证据和验收先独立推进。本轮不调用真实模型、不更新桌面。

## 本批补齐

- **公共几何与标准层。** `source_polygon_v1` 复用源 builder：非正交/凹多边形、声明圆弧及最大弦误差、共享边界统一采样、真实空间围合的楼层庭院孔洞。源 BIM 保留原曲线、离散点、宿主段来源和误差。曲线开口仅落在一条实际弦段上，仍为一个 opening，可正常保存和修改。公共模板可显式选择该合同，重复曲线/斜边楼层并展开任意直墙方向的窗组；不同轮廓、标高和例外仍由模型声明，工具不推断房间或补缺面。
- **原 mesh 截线。** `view_mesh_section` 真正计算三角面与指定水平面的交线，可精确裁剪 XY 线段；记录原面 ID、共面三角形、点接触和坐标映射，不把旧 `bounds` 的面中心筛选冒充截面，不自动封口或生成内部空间。
- **几何位置比较。** `compare_mesh_source_pixels` 在保存相机的同一视线上比较原 mesh 和源 BIM 的第一命中面，支持显式像素或有界网格。保留边缘、未命中、遮挡、有效覆盖、真实宿主和沿视线的 5/10/30 cm 残差分档。原 mesh 命中使用原三角面和 float64 局部射线重算，float32 缓冲仅用于一致性核验。无纹理窗识别、自动配准或整栋精度通过。
- **Worker 证据闭环。** 同模型只读 worker 可生成/引用截线和比较；返回真实图像、工具调用与文件哈希绑定的计算结果。父子仍使用公共 Runtime/EventStore、根预算和原恢复机制；未新建 mesh 执行循环，未引入固定角色流程。
- **首测预检。** `scripts/dev/check_mesh_readiness.py` 核验唯一原 GLB、审查接受记录、配置、输入隔离、哈希、依赖锁及版本封印，无模型调用；配置仍通过普通 `runtime_configuration` 启动。实际启动前还需当前用户对具体真实测试的授权，本次没有执行启动命令。

## 实际发现与修复

1. Windows 下比较记录的 CRLF 与不可变报告的 LF 字节不同，正常 worker 结果被哈希绑定拒绝。统一规范 UTF-8/LF 字节，保留哈希验证；真实只读 MCP、回读、跨任务复制与篡改拒绝已复核。
2. 地理坐标量级下 float32 命中坐标造成虚假的厘米级残差。改用原面与相机射线的独立 float64 求交；X=6,000,000 m 的重合/真实偏移反例通过独立复核，重合残差约数值误差量级。分档是沿视线深度差，不能当窗边框或任意方向三维误差。
3. 正式 Toolkit 入口把圆弧端部的细切片误报为不足 0.60 m 的房间，底层 export 通过而完整入口失败。按实际边的正交性限定轴向条带规则，保留同楼正交空间的 0.3/0.6 m 检查、竖向间隔及公共源几何覆盖/重叠/宿主检查；反馈明确列出未评价的非正交最小净宽。

## 验证与版本

最终开发版本 **domain-v120-20261019 / runtime-v31-20261019**。公共Runtime实现未改；451个domain文件、46个runtime文件封印。domain119保留首次入口失败所对应的登记，未覆盖旧记录。最终正式入口已通过曲线模板→原观察→worker截线/比较→证据回读→回叠→finish→同配置恢复；父6+子3共9个脚本响应、8次工具调用、180个模拟tokens，无真实模型。当前整栋交付声明有效，`fidelity=not_evaluated`；同配置恢复无新事件/请求。独立审阅本轮范围无剩余P1/P2。

| 范围 | 结果 | 时间与证据 |
|---|---|---|
| 曲线、源几何、编辑、回叠相邻 7 模块 | 96 通过 | 5.28 s；终端结果，未生成 JUnit；缓存 `D:/Sim-BIM-Agent/cache/pytest-source-curves-regression-20261019` |
| 庭院 precision 修复后曲线与 precision | 24 通过，与上行重叠 | 4.36 s；终端结果；`D:/Sim-BIM-Agent/cache/pytest-source-curves-precision-20261019` |
| 最终模板扩展 | 17 通过 | 4.19 s；`D:/Sim-BIM-Agent/cache/pytest/mesh-ready-template-cache/3152d2a1cdfa/results.xml` |
| 共享跨层、空间关系、开敞/水平围护与可见性 5 模块 | 59 通过 | 25.07 s；`D:/Sim-BIM-Agent/cache/pytest/mesh-ready-shared-geometry/99eaa3eef5bc/results.xml` |
| 独立核心审阅 6 模块 | 97 通过，非全历史集 | 12.40 s；包含几何、模板、截线、比较与预检，精度反例另列 |
| 精度修复后比较模块 | 26 通过 | 含大坐标重合、真实偏移、错误点/面拒绝 |
| 最终只读 MCP/worker 证据链 | 1 通过 | 8.70 s；真实本地 MCP，脚本输入，无真实模型 |
| 大坐标精度独立复核 | 5 通过 | 2.18 s；`D:/Sim-BIM-Agent/cache/pytest/mesh-framework-independent-precise-fix-20261019` |
| 首测预检逻辑 | 19 通过 | 6.68 s；`D:/Sim-BIM-Agent/cache/mesh-readiness-20261019.xml` |
| domain119 正式入口回归 | 12 通过、1 失败 | 78.65 s；旧 worker 链通过，新曲线链暴露上面的轴向规则误用；`D:/Sim-BIM-Agent/cache/pytest/mesh-framework-native-final/580287a2160f/results.xml` |
| 正式保存规则修复后的既有相邻检查 | 40 通过 | 首跑 `source-shape-gate/269a54221b15/results.xml`；新夹具两项缺必需footprint另记为失败，未改形状规避 |
| 新 Toolkit 形状准入反例最终复验 | 13 通过 | 5.89 s；`D:/Sim-BIM-Agent/cache/pytest/source-shape-gate-recheck/02daa8cb5053/results.xml`；独审另13通过/5.42 s |
| domain120 正式入口最终复验 | 1 通过 | 46.39 s；`D:/Sim-BIM-Agent/cache/pytest/mesh-framework-native-recheck/18cc2a0fd190/results.xml`；实际运行在其`temp/test_native_curved_templates_s0/framework` |

表中多批有重叠，不相加成唯一测试总数。检查不覆盖全历史测试集、真实 work model 能力、实际整楼速度或独立质量。所有真实模型调用均为 0。

## 首轮案例与范围

用户最新指定办公楼优先，住宅后续专项调整。此前 B212 窄高层审查仅接受其主要立面/屋顶可读性，已从首轮配置撤回，旧准备包标为 `superseded_by_office_scope`，原件与审计保留。

**框架已完成本轮离线验收，办公楼输入仍为 `input_pending`。** 本地筛查共覆盖 15 个香港单体 GLB 与 Voimatalo；其中一个香港资产因当前渲染器不支持 BLEND 材质未生成可用视图，其余以新渲染原件审查，尚无同时满足办公用途和主要立面完整可读要求的案例。Voimatalo 的办公用途已核实，但庭院底层及翼楼边缘有缺口/遮挡；进一步只用实际三角面将 12,520/12,520 个面与原城市 tile 对齐，最大质心偏差约 2.23e-6 m，同相机复查恢复的周边像素包含树木、扭曲地面和邻楼，仍不能确认存在可直接提取的完整目标外墙。未读取旧 footprint、GT 或历史 BIM，未补缺或生成替代输入。

办公准备包在 `D:/Sim-BIM-Agent/cases/mesh-office-first-full-readiness-20261019/audit/`：`README.md`、`run-config.input-pending.json`、`readiness.json`、`office-case-screening.json`、`PUBLIC_SOURCES.md` 和 `preflight-input-pending.json`。Manifest 固定 domain120/runtime31，输入目录为空；实际预检退出 1，明确 `not_ready`，不能启动。旧 B212 包也按撤回状态拒绝预检。公开赫尔辛基与香港官方资产入口及获取障碍已保存；有界检索未获得已验收的新办公 GLB，未下载大型城市包。本轮到此停止，后续先确定办公原件，再完成输入预检及单独授权的真实整案测试。

拟用 `gpt-6-sol/high`、同模型可选只读 worker、最大并发 8；全案父子共享 48 次模型请求与 240 次工具调用，每响应上限 32,000 tokens。没有整案时间或 token 硬截止；5–10 分钟为待测优化目标，不是降低质量的中止线。账号、线路可用性和真实耗时尚未在本轮验证。

首轮验收分别记录运行/用量、实际 `finish_bim` 与 `finalization.delivery`、源模型/报告/viewer、原 mesh 对应与误差覆盖、办公内部推断合理性和人工介入。范围声明、回叠、局部残差或 `completed` 均不能替代独立外形与外窗验收。GT 和旧 BIM 答案不进入模型输入。

## 仍明确不支持

单空间带孔、任意样条、连续倾斜/变截面空间、跨多个曲线弦段的单个连续开口、这些扩展几何的 EnergyPlus 适配。非正交最小房间净宽尚未可靠评价，轴向检查的 pass 只涵盖其适用部分。没有通用平立面自动匹配或跨建筑 CV 窗识别，也不以这些能力作为本次办公楼首测前置。桌面仍为 runtime24/domain110，尚未发布本批。

## 收尾核对

版本验证通过，Git 暂存中的 497 个封印文件逐一按真实字节校验，无哈希差异；证据为 `D:/Sim-BIM-Agent/cache/mesh-framework-staged-seal-20261019.json`。6 个新增测试模块均已进入检查分类，`git diff --check` 通过。本轮没有新建工作树，既有 Go 工作树未动。收尾磁盘剩余 C 44.38 GiB、D 120.25 GiB；案例与原始检查输出仍在本机外部目录，Git 保存程序、小报告和证据索引，不代表大证据已远端备份。
