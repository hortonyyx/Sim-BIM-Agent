# Mesh 串行简单几何与减少往返迭代

本轮基于`8994440f`实施。用户接受上轮candidate_06整体结果，优先时间，并最终明确：“由粗到细＋基本几何与关系表达＋减少往返”，串行仍为主方向；并发需要共同讨论分工和角色体系。本轮未改worker、并发、模型或预算，没有真实模型调用、新整案或桌面发布。

## 实现与边界

1. 参数入口支持`primitive_frames`和单空间`primitive.rects`。在局部轴内做矩形并集，再转换到源XY；旋转矩形不再需要模型逐角计算。并集必须连通、无洞且有效，拒绝点接触/断联，不拆房或新增内墙。尺寸、旋转、邻接及真实例外仍由模型结合证据声明，不自动拟合原网格。
2. `local_baseline`可用于门、opening_rows、普通/核心连接；保持显式两侧ID及Z规则。共享墙采用共同的法向与切向锚点，门宽和opening_rows的米制span不按各宿主长度比例缩放；只检查窗组实际开口，空白基线不是开口。原始越界、采用后越界及双方不一致都拒绝，不裁剪/删门/自动换宿主。
3. 纯line `boundary_path`用公共曲线展开得到环，进入XY/窄体量检查，再恢复共享ID、方向及开口fraction。所有引用必须给出一致端点；与真弧模板共享且会移动的直线拒绝。真弧模板XY仍未评，普通source校验照常。generic修订同步曲线、派生polygon/bounds与缓存；有效局部轴继承到后续修订。
4. 独立环问题分阶段集中反馈，锐角位移提供最坏顶点、原/新坐标、支持线与最大位移。被前阶段阻塞的宿主/标高/接触检查明确`blocked_by`，不使用半采用结果。本轮不是全编译器所有错误的全量聚合，跨模板/接触等下游仍可能在首个相关错误停止。
5. Mesh指引先基本形/关系，再按需量测；同一建筑判断集中处理相关参数，提前识别基座/屋顶重大例外，按需局部复核。真实斜边、弧、退台与重要空间关系保留；不将简单形强制成所有对象的矩形，不增加固定阶段。

单模板只有一种primitive轴时采用该轴，显式`modelling.frames`优先。混合primitive轴模板未指定规整轴时用identity并给诊断；模型应在需要时显式声明。原始draft/plan保持，采用稿、审计、proposal各自绑定哈希。重复规整可能出现约1e-12m浮点差，几何比较用1e-9m容差，身份/结构/哈希仍分别严格校验。

## 验证记录

最终开发版`domain-v128-20261020`、`runtime-v34-20261019`，登记/校验均通过，仅domain变更。所有检查为离线，真实模型调用和模型tokens均为0。上轮真实耗时29分41秒只是基线，本轮没有新的完整耗时或5/10/30cm质量成绩；不能由单元通过、旧稿重放或表达变短换算15分钟达标。

| 定向范围 | 最终结果 | pytest耗时 | 证据 |
|---|---|---|---|
| 新primitive与既有parametric proposal两个模块 | 73 passed | 4.36s | 首轮pytest输出，临时产物`D:/Sim-BIM-Agent/cache/pytest-primitives-20261020-02` |
| regularization、host、line paths、representation integration、独审反例、mesh policy、source curves七模块 | 77 passed | 10.11s | [JUnit](D:/Sim-BIM-Agent/cache/pytest-serial-20261020-final.xml) |
| 正式参数MCP stdio、参数函数/补丁、按需reference子集 | 39 passed，40 deselected | 21.99s | [JUnit](D:/Sim-BIM-Agent/cache/pytest-serial-tools-20261020.xml) |
| 检查清单、版本、diff | unclassified=[]；register/verify与diff --check通过 | 未单列 | 新5个测试模块已归入targeted |

三组pytest选择互不重复，合计189项有效通过，未运行完整/历史套件。最初host/line组合41通过、2失败：一个为opening_rows空白基线被当实际开口导致过严，已改为按物理span验证；另一个为测试锁死blocked字典形式，已改为语义检查。primitive首轮Windows GBK读取测试产物失败已用UTF-8方式修正；独审反例一次因外部basetemp父目录未创建导致setup错误，补目录后通过。这些失败和修后复验不重复计入189项。

独立审阅交叉进行：primitive实现由非实现者审阅；line adapter由另一位审阅者检查；根实现的共享宿主、聚合与generic写回由独立审阅者检查。发现path-only generic cell残留旧bounds的P2，以及重复顶点仍首错返回的覆盖缺口，均修复并加入相应反例；混合frame回退也补明确诊断。

最终交叉复核无未解决P1/P2。没有创建开发工作树，也未终止其他会话或清理旧run。收尾磁盘余量C盘46.44GiB、D盘117.35GiB；测试缓存/历史重放证据位于D盘，桌面产品保持原安装。

### 历史草稿只读重放

原run为`D:/Sim-BIM-Agent/runs/lee-garden-six-mixed-61sol-scale-20261020-001`。重放仅调用当前确定性预编译，不重新建楼或修改原稿。证据：[summary.json](D:/Sim-BIM-Agent/cache/serial-coarse-replay-20261020/summary.json)、[完整replay.json](D:/Sim-BIM-Agent/cache/serial-coarse-replay-20261020/replay.json)。保护的50个草稿目录文件及479个源码文件前后哈希一致；该次工作树内容摘要`9dbc92176cf5952f1534e996160ef981e4ef79e7a5f3d779d98f2278b35635a5`。

| 草稿 | 原结果 | 本轮确定性预编译 |
|---|---|---|
| 001–006 | 首处轮廓位移失败 | 分别一次报告8/6/6/6/4/2处位移问题，附最坏点，阻断依赖检查 |
| 007–008 | shared_host_conflict / ambiguous_aperture_host | 越过原首错，仍拒绝真实volume_contact_changed |
| 009–010 | cross_template_host_conflict | adopted；仅预编译通过，未据此构建/验收整楼 |
| 011 | 旧audit adopted、实际build_rejected | 6个line模板/12路径环进入，拒绝F3共享线端点冲突 |
| 015 | 旧built/candidate_05，后续source修订成candidate_06 | 7个line模板/17路径环全部进入；楼梯footprint与space两处超限，最大0.7443m |

旧11次业务失败包含10次规整失败和第011次后续构建失败。第015次是最后参数稿而非最终source-only候选。补覆盖会揭示原先被跳过的问题，不代表自动修好历史结果；需要新粗到细声明避免不必要的尖角/微小歪斜。12次本地预编译合计0.739秒不是受控提速对照，不能与整案29分41秒比较。

## 后续

下一次先按用户新授权，用同三输入、同模型/预算冷启动对照首次有效候选、父模型请求数、返工次数、全案时间和已接受质量。串行路径的实测结果再决定是否需要轻量并发；分工、角色、消费时机和部分结果回交一起讨论。并发设计、异常支线向参数计划的通用合并、自动量测选择、真弧XY规整和完整构件建模不在本轮。
