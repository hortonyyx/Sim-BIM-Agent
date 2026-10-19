# Go 协议路径与 Paratera 图片参数修复

用户在查看旧 textured mesh 候选期间，要求先修 Go 与 Paratera 连接。本轮只改 runtime 接线，未改 mesh、domain、读图方法或模型默认配置。开发版为 **runtime-v30-20261019 / domain-v113-20261018**；固定桌面仍 runtime24/domain110，本轮未发布。

## 结论与修复

1. **Go：推理路径与当前官方协议路径不一致。** 原来把订阅/目录 base `/inference/go/v1` 直接用于三种推理协议。现按[官方端点说明](https://dev.opencode.ai/v2/docs/console/go/#endpoints)使用 `/inference/go/openai/v1/chat/completions`、`/inference/go/openai/v1/responses`、`/inference/go/anthropic/v1/messages`。凭据的订阅范围和 `/go/v1/models` 目录入口保留；公开连接描述记录实际推理 base，传输器拒绝目录 base、错协议及非订阅端点。没有凭据搜索、环境回退或换线。
2. **Paratera：本轮 400 有确定的图片枚举错误。** 同一请求经系统代理与直连均返回 400，服务明确指出 `messages.content.image_url.detail=original` 非法，只支持 `low/high/auto`。现仅对 Paratera 将本地 `original` 提示投影为 `high`，初始图片和工具回图都处理；在保存工具呈现前完成转换，避免呈现审计与发送内容不一致。原 MCP 回执、原来源、图片字节/哈希及提示变换记录保留。没有裁剪或压缩图片；`high` 不意味着服务实际按原像素解析，读图质量仍需另验。
3. **不把上述问题与全部历史中断等同。** 原 Go 非流式长请求在等待响应头时 EOF；串行、新建连接及较小请求也失败，不能只归因于并发、连接复用或包大小。新路径小请求成功，尚不能证明此前长请求 EOF 已消失。Paratera 历史 450 秒取消来自 reader 的共享预算分配，不是本轮发现的图片枚举问题；本轮未扩大超时、增加重试或更改全局代理。历史某次相同请求 400 后又 200 的原因仍未完全解释。

系统代理仍为本机既有设置。Go、Paratera 的目录查询分别在系统代理与直连下均 HTTP 200（37/82 个型号），这只证明当时基本连通，不用于判定长请求由哪个网络节点中断。旧运行仍受版本与路由一致性约束，新验证使用新目录，不重写旧证据。

## 真实连接验收

角色为只读 `connector-check`；初始 128×128 绿色合成图，工具返回 128×128 红图和随机 nonce，模型需正确回答两种颜色及 nonce。两路分别使用 GLM-5.3-Flash / high，输出上限 8,192、每次最多 2 请求/1 工具/50,000 tokens，根时间 900 秒、单请求 450 秒，0 自动重试。没有建筑图纸、GT、mesh、BIM 组装或整案。

| 项目 | 版本 | 请求/工具 | 结果 | 耗时 | 供应商实报 tokens |
|---|---|---:|---|---:|---:|
| Go `glm-5.3-flash`，新官方 Chat 路径 | runtime29/domain113 | 2/1 | 两次 HTTP 200，颜色及 nonce 正确，usage 完整 | 4.047 s | 649 |
| Paratera `GLM-5.3-Flash`，修复前 | runtime29/domain113 | 1/0 | 初始图片 HTTP 400 | 2.078 s | 未知 |
| Paratera 同字节直连诊断，修复前 | 原请求 SHA 绑定 | 1/0 | HTTP 400，明确拒绝 `original` | 2.203 s | 未知 |
| Paratera `GLM-5.3-Flash`，修复后 | runtime30/domain113 | 2/1 | 两次 HTTP 200，颜色及 nonce 正确，usage 完整 | 18.500 s | 596 |

合计 **6 请求、4 成功响应、2 次本地工具调用、已知 1,245 tokens，另 2 请求 usage 未知**；错误响应未提供 usage，不能记成零消耗。Go 按订阅记录，不套人民币单价。Paratera 完整费用估计不可得，成功请求只有已知费用分量 0.0005288 元，不当作完整账单。图片预算估计与供应商实报用量分别保留。

两个成功闭环的请求图片数均为 1→2，原图和发送图片 SHA 相同。Go 实测 runtime29；runtime30 只增加 Paratera 参数投影，Go 在最终版通过离线相邻验证，未重复真实调用。Messages、Responses 本轮只做离线线路验证；没有实测其他 Go 型号或长时间/大图/并发稳定性。

## 离线检查与独立审阅

| 范围 | 结果与耗时 | 边界 |
|---|---|---|
| Go 路由/签名/订阅定向检查，runtime29 | 62 passed，22.12 s | 与后续检查重叠，不累加 |
| Paratera 真实错误的最小反例 | 修复前 1 failed（`invalid_request`），修复后 1 passed，1.12 s | 同时覆盖原图、工具回图、来源与字节、输入不变 |
| 最终 8 模块相邻检查，runtime30 | 初跑 169 passed / 4 failed，35.64 s | Go、订阅、通用 runtime、图片引用/预算/工具证据、上下文 |
| 失败项定向复核，UTF-8 启动 | 1 passed / 3 failed，1.35 s | 1 项是 Windows 默认编码读取源码导致，UTF-8 下通过 |
| 修改前 `36a91732` 独立源码副本复核 | 同样 3 failed，1.23 s | StopIteration 身份断言 1 项、旧上下文终止状态断言 2 项，均为既有失败 |
| 独立审阅者检查 | 2 passed；另一个上下文淘汰/重读场景完成 | 4 次脚本响应、3 次本地工具；`high`、相同图片字节及 `store.validate()` 均通过 |
| 版本登记/文件哈希 | runtime30/domain113 验证通过 | domain 未变 |

最终有效覆盖为 **170 项通过、3 项已在未改基线复现的失败**，不称整个检查集全绿。未为本任务扩修无关历史失败。最初误选历史测试文件被收集边界拒绝、Paratera 新反例的初始输出预算设置被既有最小值拒绝；均修正执行选择/夹具后再验证，不当作产品新缺陷。直接使用系统 Python 时缺少 pytest，实际测试使用仓库 `.venv`。

独立审阅未发现 P1/P2 阻断问题；覆盖路由、来源投影、工具呈现、上下文回读、旧运行恢复边界和版本哈希，0 真实模型调用，未参与实现。该结论不代表模型读图质量或外部服务长期稳定。

## 证据与恢复

- 真实运行根：`D:/Sim-BIM-Agent/runs/connection-repair-20261019`，分别保留 `opencode-go`、`paratera`、`verification-v30/paratera` 的原始事件、请求/响应 blobs、传输 trace、receipt 与 summary；索引 `evidence-sha256.json`。
- 诊断 `paratera/direct-diagnostic.json` 绑定请求 SHA `42fd6d32ed55e89386b9d36d3b9946de737c0d7476f06485df5335fde929ca54`；只保存经脱敏的错误内容。
- 离线结果与验证脚本：`D:/Sim-BIM-Agent/cache/connection-repair-20261019`；`final-tests.xml`、`failures-utf8.xml`、`baseline-tests.xml`。独立上下文检查：`D:/Sim-BIM-Agent/cache/paratera-context-review-cg5dlo0f`。
- 证据目前在本机 D 盘，未声明远端独立备份；Git 只提交程序、回归与短报告，不提交凭据和大运行输出。

| 汇总文件（相对运行根） | SHA-256 |
|---|---|
| `opencode-go/summary.json` | `c54b3a9ff88f2f8bcc1bf94aa3333c25eb2e19386dd260dceb74e7e16462a93a` |
| `paratera/direct-diagnostic.json` | `952019a059124a98ac1ceed24eb3bd17831456c4535b3106aef7672a02b6cf13` |
| `verification-v30/paratera/summary.json` | `e3a17924f6b6f7e1ccfd9f222b3c2b55e13ebd08217845896f3d756437ad33c6` |

本轮没有新建开发工作树。收尾时 C 盘剩余 45.89 GiB、D 盘 121.76 GiB。mesh 等用户看完旧候选后再讨论；没有启动新的建模或改动旧候选。
