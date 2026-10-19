# Go 独立物理直连与完整读图复测

2026-10-19，用户授权将 Go 改为真正直连、不影响其他服务代理，并完整复测之前中断的读图任务。mesh 保持不动。

已完成实现及三个完整冷启动复测：31请求、29完整HTTP200、40工具；另2请求因原节点时间预算截止取消，**0提前EOF、0 HTTP错误**。15个完整响应超过60秒，最长281.717秒。Go及其DNS有逐请求物理出口证据；Paratera测试前后均仍经原10809代理正常返回目录。两个平面任务未正式提交，MiMo北立面正式提交；连接结果与读图质量分别列明。

## 已完成的连接改动

实现提交 `55d02b1b`，runtime31/domain113。只有 Go 使用物理 IPv4 transport；其他 provider 的 `trust_env=True` 默认行为保持。显式 Go 私有连接文件增加公开 DNS bootstrap 参数，原凭据字段均保持，不读取环境变量作回退。

Go 与 Cloudflare DoH 的 socket 在 connect 前选择动态发现的物理接口并绑定源 IP，原 Host、TLS SNI/证书验证保持；不改全局代理、TUN、hosts 或系统路由，不固定 IF9、源地址或 Go 服务 IP。动态接口选择依据物理网卡有效默认路由的 metric 总和，歧义/无可用网卡/直连失败时报错，不悄悄回代理。本实现当前针对 Windows 物理 IPv4，其他平台不会伪称已绕过 TUN。

DNS 同样走物理接口。默认 bootstrap `1.1.1.1` 在现场直连超时；已在仓库外 Go 文件显式设置经核验可达的 Cloudflare DNS 地址 `104.16.248.249`，仍用 `cloudflare-dns.com` 验证证书。这是 DNS 引导地址，新建连接且地址缓存过期时重新解析Go的A地址，缓存期限最多300秒；已建立连接可继续复用，不按TTL强制断开。DNS/网卡发现/连接都计入原请求预算，无模型重试/供应商切换。公开配置说明见 [Go工作流](../workflow/opencode_go.md)。

依据：[Microsoft socket 出接口](https://learn.microsoft.com/en-us/windows/win32/winsock/ipproto-ip-socket-options)、[HTTPcore 网络后端](https://www.encode.io/httpcore/network-backends/)、[Cloudflare DoH JSON](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)。

## 实施验证

| 检查 | 结果与范围 |
|---|---|
| 离线定向 | `test_go_direct`、`test_opencode_go`、`test_runtime_subscription`、`test_role_configuration` 共104通过/11.82秒，0模型调用 |
| 关键覆盖 | Go/其他provider代理隔离、动态接口选择、fake-IP拒绝、DNS错误分类、DNS期限、取消关闭、原TLS主机/证书验证与精确wire、现有三个Go协议/图片工具闭环 |
| 首次检查失误 | cache父目录尚不存在，18个测试setup错误、27通过；建目录后原范围48通过，再随DNS形状修复完成最终104通过，不当作产品缺陷 |
| Go真实目录检查 | 200/37型号，5.922秒；DNS与Go均记录IF9物理socket和真实peer，`trust_env=false` |
| Paratera代理保持检查 | 测试前后均200/82型号；`trust_env=true`，实际TCP仍连`127.0.0.1:10809`，0推理调用；后检证据在本批cache的`other-provider-proxy-postflight.json` |
| 独立审阅 | 无剩余P1/P2；发现的DNS JSON形状错误分类已修并复核，子进程和DNS取消回收亦离线核验；审阅者0网络调用 |
| 版本 | runtime31覆盖46文件，domain113的442文件保持；改动文件Git staged字节与已验证checkout一致 |

实施证据目录：`D:/Sim-BIM-Agent/cache/go-direct-20261019`。真实目录检查与网络可达性探测不计推理调用；未调用其他新模型。

## 授权的完整复测

固定已提交 runtime31/domain113，三个 Go reader_unit 串行冷启动：GLM `glm-5.3-flash/high` 首层、MiMo `mimo-v2.6-flash` 默认思考的首层及北立面。每任务900秒、32请求、128工具、32k输出，0重试，无额外总tokens/金额上限；通常单请求450秒，仍受节点剩余预算限制。

来源为前次 `reader-connection-retest-20261019` 的对应三个配置。case 内只改变输出目录，原 task ID/角色/任务说明/坐标约定及六张原图保持，不供给GT/旧模型答案，不人工提示或改稿，不组装整楼/运行mesh。完整复测指允许原读图循环运行至正式提交或原有预算/失败终态，不能保证模型必定成功交付。

运行目录：`D:/Sim-BIM-Agent/runs/go-direct-reader-retest-20261019`。缓存/启动脚本：`D:/Sim-BIM-Agent/cache/go-direct-reader-retest-20261019`。被动观察器保留请求体SHA、原超时/阶段，并保存生产transport内的DNS/模型实际物理连接证据，不修改正文或代理选择。

## 完整复测结果

| 模型/角色与案例 | 耗时秒，含启动收尾 | 请求/完整响应/工具 | 已知tokens；未知请求 | 完整响应>60秒；最长秒 | 最终结果 |
|---|---:|---:|---:|---:|---|
| GLM high / plan_reader / F1 | 904.326 | 10 / 9 / 12 | 598,951；1 | 6；281.717 | 900秒节点截止，4次trial失败/拒绝，无有效稿或正式提交 |
| MiMo默认 / plan_reader / F1 | 908.607 | 12 / 11 / 15 | 378,111；1 | 6；179.915 | 900秒节点截止，1份通过程序校验但未提交的草稿；独评质量失败 |
| MiMo默认 / elevation_reader / North | 638.825 | 9 / 9 / 13 | 256,466；0 | 3；195.248 | 正式提交，本张数值及实际图独评通过 |
| 合计 | — | 31 / 29 / 40 | 1,233,528；2 | 15；281.717 | 3节点全部终态，1正式读图提交，无整楼交付 |

Go按订阅记账，上表为服务实报token（包含供应商计入的图像和缓存），2个取消请求usage未知，金额未知，不把未知用量填零。0自动重试/供应商切换、0整案/mesh、0人工改稿；独立审阅和终态评价均0新模型调用。

GLM最后请求仅剩约2.988秒，被900秒节点截止取消，未发生提前EOF。4次trial依次因尺寸链刻度比不一致、两个seed落同一空间、操作缺value/changes或id不合法、南侧开口w-south1未找到覆盖其全长的边界宿主而失败/拒绝；3份保存的失败稿均未通过validation，0有效trial/正式提交。5/10/30cm、墙厚、净图与回叠质量未评，未生成正式BIM或viewer。独立审阅逐项核对上述结果。

31/31请求wire SHA与运行日志一致，物理出口证据覆盖31/31请求：三节点合计4次DNS、4次Go TCP连接均在物理IF9，复用连接沿用已验证出口，未经过代理/fake-IP。各节点532份运行源码、配置及原图均保持；串行时间区间不重叠。全局网络配置核验中，`clash-verge.yaml`、`config.yaml`、`dns_config.yaml`、`verge.yaml`哈希保持；`profiles.yaml`在13:43:25发生外部写入，操作者及具体变化未知，不能声称所有Clash文件逐字未变。本轮没有修改这些文件，不恢复或覆盖该写入；最终实际Paratera连接仍走10809代理并返回200。

上轮相同任务在GLM第5请求45.481秒、MiMo F1第1请求60.219秒、MiMo North第3请求44.599秒出现`RemoteProtocolError`。本轮三节点按请求序号都越过原失败位置，但后续模型历史/正文不同，不等同于原失败payload逐字复放；全部完整响应中没有该错误；两个取消由本地时间预算解释，不能归作代理断流。结合先前同一请求A/B，证据支持保留Go独立物理直连，但不能由这3个样本证明长期稳定，或进一步定位旧代理路径中究竟哪一跳有问题。

## MiMo首层未提交草稿的诊断

MiMo首层908.607秒终态（含启动/收尾）：12请求/11完整响应/15工具，已知378,111tokens另1请求未知。6个完整响应超过60秒，最长179.915秒；末请求仅剩约26.120秒，被节点预算取消，未发生EOF。12/12正文SHA、字节数和物理出口证据均由独立审阅核对；首请求正文与上轮相同，本次27.562秒完整200，上轮60.219秒EOF。后续模型历史不同，不能把全部请求当作逐字节A/B。

前三次trial分别因11个inferred room role缺显式assumption、中央墙段悬挂、开口W-S2无完整边界宿主失败；第四次通过二维程序校验，`reading_geometry_ready=true`、`source_geometry_ready=false`。没有调用正式提交，也没有源BIM或viewer。以下仅评价保存的未提交trial，不能当作正式交付验收。

| 未提交草稿的指标 | ≤5cm | >5–10cm | >10–30cm | >30cm |
|---|---:|---:|---:|---:|
| 14个匹配房间的边界Hausdorff差 | 0 | 1 | 0 | 13 |
| 30个匹配开口的沿向端点差 | 0 | 0 | 6 | 24 |
| 30个匹配开口在平面内垂直宿主墙线的位置差 | 7 | 2 | 9 | 12 |

参考/草稿房间数均14且能匹配，但形状和连通不通过：s-central1吞入西北横走廊，s-central2吞入西南入口通道，中央西边界偏左，多处门窗不贴原开口。原图、实际净图/回叠及独立回叠已由主助手和独立审阅者查看，视觉与数值问题对应。走廊最大Hausdorff差9.107m是形状被改变的度量，不是整图平移9m。31个参考开口中匹配30、漏DX_WS；另多出W-S1、D-bottom-west两个开口。宿主匹配27/30，沿墙方向最大差3.639m，平面内垂直宿主墙线的位置差最大0.836m（不是Z高度误差）。28/28墙段填写厚度且全部标为inferred，仅确认字段覆盖，厚度准确性未独评。

结论为**未提交草稿质量不通过**。评价发生于该节点终止后，0模型调用、0生成侧改稿；804份保护哈希及532份源码/配置均复核保持，GT与独立标定只用于评价。

评价目录：`D:/Sim-BIM-Agent/cache/go-direct-reader-retest-20261019/mimo-flash/evaluation-f1-04`，`quality.json`保留原始计算结果，`visual-review.json`另记实际图审阅。入口：[实际回叠](D:/Sim-BIM-Agent/cache/go-direct-reader-retest-20261019/mimo-flash/evaluation-f1-04/reader_view_1.png)、[实际净图](D:/Sim-BIM-Agent/cache/go-direct-reader-retest-20261019/mimo-flash/evaluation-f1-04/reader_view_2.png)、[独立回叠](D:/Sim-BIM-Agent/cache/go-direct-reader-retest-20261019/mimo-flash/evaluation-f1-04/independent_overlay.png)。

## MiMo北立面正式读图

本张638.825秒完成，9请求/9完整响应/13成功工具，256,466tokens且usage完整；最长请求195.248秒，3次完整响应超过60秒，0传输失败或重试。工具为inputs 1、view_image 4、view_pixel_profile 5、view_pixel_region_overview 1、trial_elevation_reading 1、submit_elevation_reading 1。模型从原图和尺寸/像素证据完成二维立面并正式提交，无人工几何修改。638.825秒保留全部模型等待时间，不扣除服务延迟，也不宣称速度目标达标。

| 独立数值指标 | ≤5cm | >5–10cm | >10–30cm | >30cm | 最大差 |
|---|---:|---:|---:|---:|---:|
| 8窗采用位置 | 8 | 0 | 0 | 0 | 0.00360m |
| 8窗原始观测位置 | 8 | 0 | 0 | 0 | 0.01377m |
| 8窗宽度 / 高度 | 8 / 8 | 0 / 0 | 0 / 0 | 0 / 0 | 约0 / 0.00002m |
| 8窗实际回叠位置 / 高度 | 8 / 8 | 0 / 0 | 0 / 0 | 0 / 0 | 0.00360 / 0.01281m |
| 3条层线实际回叠 | 3 | 0 | 0 | 0 | 0.01358m |
| 闭合可见轮廓实际回叠 | 1 | 0 | 0 | 0 | 0.01358m |

上下层各4窗，8/8匹配、无漏窗/多窗，0/3.6/7.2m层线和可见轮廓完整。主助手检查实际净图、实际回叠及独立回叠；独立审阅者另看原图和全部5张评价图，确认无可见错层、漏窗或明显错位。**通过本张二维立面质量检查**，不外推其他立面、三维开口宿主、整楼交付或跨次稳定性。原图中10000/15000分界竖线的语义仍作为模型未决项保留；该读图节点不负责解释整楼内部隔墙。墙厚不在立面评价范围。

终态后评价保护672份run文件不变，GT和独立标定只在评价侧使用；0新增模型请求。原始`quality.json`/`review.html`保留生成时“待视觉审阅”字段，后续审阅写在补充`visual-review.json`及本报告，不追改原始计算结果。

评价目录：`D:/Sim-BIM-Agent/cache/go-direct-reader-retest-20261019/mimo-flash/evaluation/north-04`。入口：[实际立面净图](D:/Sim-BIM-Agent/cache/go-direct-reader-retest-20261019/mimo-flash/evaluation/north-04/reader_elevation.png)、[实际原图回叠](D:/Sim-BIM-Agent/cache/go-direct-reader-retest-20261019/mimo-flash/evaluation/north-04/reader_overlay.png)、[独立回叠](D:/Sim-BIM-Agent/cache/go-direct-reader-retest-20261019/mimo-flash/evaluation/north-04/independent_actual_overlay.png)。reader_submission保存在该run的独立task目录，未生成源BIM/viewer。

## 证据与收尾边界

- 实现：`55d02b1b`，runtime31/domain113；三个节点从同一提交冷启动，source/input/config均保持。连接代码审阅发现的DNS JSON形状错误已修复并复核，无剩余P1/P2。读图评价不改该版本。
- 本批cache的`batch-plan.json`、`batch-progress.json`保存授权、配置差异和3节点终态；`audit.json`、`route-audit.json`保存传输、工具、用量与出口汇总；每节点`*-preflight.json`、`*-launch-result.json`、`*-transport.jsonl`及runs内原始事件/收据可逐项复核。`evidence-manifest.json`保存本批2,180份cache/run证据哈希，含补充视觉审阅；清单SHA256为`52edbc96244608f8695174dc986b03547943a67bd0b90eb1420015ffa5dedf81`。
- 完整复测已结束，未自动加时、重试、换模型或修reader/domain。保留6 Sol主线；本轮仅排查连接和复测获准的3节点，mesh未开始。
- 结果限于当前Windows物理IPv4及已配置DNS bootstrap；物理网卡/网络环境改变后需重新核验可达性。直连失败明确报错，不回代理。29次完整响应和1次正式读图交付不构成长时间、多网络或所有Go模型的稳定性承诺。
- 本轮改动在D盘开发版，固定桌面仍runtime24/domain110，未作发布。没有新建工作树，已有`codex/opencode-go`独立工作树保留。本地原始证据不因Git推送等同远端备份。收尾空间：C盘46.89GiB、D盘121.66GiB。
