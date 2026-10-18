# 国产模型候选与textured mesh收工交接

2026-10-18。用户决定暂停读图员、27B和整案迭代，下一轮转向搭建textured mesh harness。国产模型本轮只调研，之后再尝试；选型不局限本地级，优先更快、更低服务档位和整任务成本。

## 当前保存状态

- 开发版runtime24/domain113，固定桌面runtime24/domain110；上一轮提交`17b5a716`，核心实现`4b6854a2`。本轮只改文档，不更新桌面或版本封印。
- 6 Sol仍是图纸质量基线。其一平一立结果、Haiku/27B失败、耗时与tokens见[上一轮交接](2026-10-18_reader_lean_and_model_ceiling.md)。3–4分钟平面目标尚未稳定实现，最终版未做全图复测或完整独立质量审阅。
- 墙厚、10cm规整、净图/回叠、8并发、缓存参数和调度员完整信息权限保留。停止为27B增加提示或专属格式修补。

## 国产模型候选

以下是根据2026-10-18官方资料作出的后续试验顺序，不是建筑读图能力排名。原生图像输入、Function Calling和结构化输出是入口条件；运行时还须能让模型继续查看工具返回的局部图、净图和回叠。OCR分数、峰值tokens/s及Flash名称不能替代整张图的质量与总耗时。

| 顺序 | 候选 | 官方依据与选择理由 | 后续重点 |
|---|---|---|---|
| 优先1 | 豆包Seed 2.0 Lite，候选快照`doubao-seed-2-0-lite-260428` | [Seed官方](https://seed.bytedance.com/zh/seed2/)将Lite定位为质量与响应速度的平衡档；[模型目录](https://docs.volcengine.com/docs/82379/1330310?lang=en)列出多模态、工具和结构化输出 | 先看细小尺寸、墙窗定位及自审纠错；Lite通过后再考虑Mini |
| 优先2 | `qwen3.8-flash` | [百炼官方模型页](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)明确支持图像、Function Calling、结构化输出及OpenAI/Anthropic接口 | 单独核验官方路由；不是本轮Paratera `Qwen3.8-27B`，其失败不直接外推到Flash |
| 后备1 | Step 3.7 Flash | [官方模型页](https://static.stepfun.com/blog/step-3.7-flash/)提供原生视觉、工具编排，以及裁图/缩放配合的细粒度视觉评测；196B语言参数加1.8B视觉编码器、11B激活 | 能力方向贴近观察—工具—复核；仍须实测工具参数稳定性、图纸空间关系及服务延迟 |
| 后备2 | `glm-5.3-flash` | [官方API说明](https://docs.z.ai/guides/vlm/glm-5.3-flash)列出原生图像、Function Calling、结构化输出，320B总参数/18B激活 | 思考不能关闭，需看实际思考量；FlashX为另一个速度档，官方称200tokens/s，但套餐入口不同，不默认切换 |
| 进一步降档 | 豆包Seed 2.0 Mini，候选快照`doubao-seed-2-0-mini-260428` | [同一官方模型目录](https://docs.volcengine.com/docs/82379/1330310?lang=en)支持多模态与工具；[Seed官方](https://seed.bytedance.com/zh/seed2/)强调吞吐和部署密度 | Lite先建立合格基线，再判断Mini能否承担立面或完整平面，不能预设复杂平面够用 |
| 观察 | MiniMax-M3.1-Flash-Preview | [官方托管调试页](https://solutions.minimaxi.com/debug/text)展示图像输入、思考及M Plan密钥条件 | 稳定API合同、工具返回图的闭环及该精确型号价格尚未充分核实，先不排在正式候选前面 |

以上优先级是任务适配判断。MoE激活参数小不代表整套权重能在小显存部署，也不等于与稠密同参数模型属于同一能力级别；本轮先比较服务档位与完整任务成本。

豆包[Responses API](https://docs.volcengine.com/docs/ark/create-model-responses-api?lang=zh)文档包含函数工具及图像内容回传。其他候选的模型能力声明同样不替代本项目真实多轮图片闭环验收。正式尝试前应核实具体区域、模型ID和账号入口，不因同品牌已有provider就默认可直接替换。

### 公开价格参考

人民币每百万tokens，输入为未命中缓存，输出另计；仅作服务档位筛选，非单图成本估算。

| 候选 | 输入 | 输出 | 适用口径 |
|---|---:|---:|---|
| Qwen3.8-Flash | 0.8 | 2.7 | 百炼华北2北京原价；新加坡国际为1.094/3.427 |
| Seed 2.0 Lite | 0.6 / 0.9 / 1.8 | 3.6 / 5.4 / 10.8 | 单请求输入长度≤32k、32–128k、128–256k三档，非音频 |
| Seed 2.0 Mini | 0.2 / 0.4 / 0.8 | 2.0 / 4.0 / 8.0 | 同上三档，非音频 |

来源：[百炼模型定价](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)、[火山LAS大模型计费](https://docs.volcengine.com/docs/LakeAIService/Largemodelbilling?lang=en)；后者声明沿用方舟在线token标准价格。长上下文、思考输出、返工、缓存和区域会改变实际费用，不按最低输入价推算整案收益。GLM、Step及MiniMax精确路由的价格留到选定入口时核对。

DeepSeek现行Flash仍支持视觉与工具，见[官方价格与能力表](https://api-docs.deepseek.com/quick_start/pricing/)。项目已测过该路线，保留[平面失败、部分立面可用的证据](2026-10-17_deepseek_reading_nodes.md)，不因官方升级或榜单自动重跑。Kimi等旗舰档暂不列首批，先集中比较上面的Lite/Flash档；没有对其图纸能力作否定结论。

### 以后恢复读图测试时

每个新候选先固定同一原图平面/立面各一张、同一harness与质量口径，GT和旧答案留在评价侧。先确认图片—工具—工具返回图片可用，再看墙/空间/开口/宿主/连通、实际回叠和自主纠错。报告首稿时间、完成时间、返工次数、全部tokens与实际费用，服务失败单列。合格后再做降思考和跨图泛化，不为某型号加专属流程。本段是后续方案，本轮不启动。

## 下一轮textured mesh入口

目标是让一个已有textured GLB经过观察、尺度/方向确认、几何与语义判断，交付可核验的Lite源BIM与现有查看器。先补齐这个自主闭环，再决定照片生成和混合输入的接入方式。

现有可复用入口：

- [mesh工具](../../scripts/tool_scripts/bim_agent_mesh.py)：自包含GLB冻结、`inspect_mesh`、`inspect_mesh_directions`、`view_mesh`及`measure_mesh_pixels`。方向统计和贴图像素不是现成墙/房间语义。
- [观察实现](../../src/agent/geometry/mesh_observation.py)与[mesh/BIM坐标映射](../../src/agent/geometry/mesh_bim_frame.py)：材质/贴图读取、视图与表面坐标查询、明确frame的回叠。
- [照片原型记录](../design/photo_to_mesh_bim.md)：原始及尺度处理GLB、参数、脚本、源BIM和查看器位于`D:/Sim-BIM-Agent/runs/photo-figure15-hunyuan-20261014-01`。

下一轮先盘点上述能力与实际输入的差距，再搭建最小流程。需要明确的产品选择是：仅有外部mesh时输出到哪种空间细度，未知尺度怎么表达，不可见室内及背面如何保留假设。不能默认一层一区，也不要求从外观虚构房间级还原。

既有Figure 15原型含开发代理介入、生成补全、初始80m高度假设及后续外部面积/窗墙比标定；它可用于工具和产物对接，不能当独立几何真值或自主成功基线。圆弧/斜边的现有编辑支持须另查。先复用当前runtime、domain与源BIM，不预先增加全模态调度框架或新角色。

## 本轮验证边界

本轮仅官方网页调研、仓库只读核对及文档收尾：0新增work model推理、0读图、0整案、0图像/mesh生成、0程序变更、0测试执行。文档检查包括相对链接、唯一当前交接和Git diff；未发布桌面。旧验收结果仍归属其原版本和批次。
