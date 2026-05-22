# 评分标准

总分 100 分。

## 环境阅读与需求理解：15 分

- 13-15 分：能结合需求并在当前项目环境中识别 WebSocket chat transport、React hook、abort/resume tests、resumable streaming 文档和 agent chat 生命周期。
- 8-12 分：能识别刷新页面不应取消服务端任务，也能阅读部分项目路径，但对显式停止、配置策略或恢复兼容分析不足。
- 0-7 分：未有效阅读当前项目环境，只泛泛描述“增加取消按钮”或“断线重连”。

## 技术问题识别：15 分

- 13-15 分：准确指出问题本质是客户端本地生命周期与服务端 durable turn 生命周期被错误耦合，以及显式取消与本地清理需要分离。
- 8-12 分：能指出刷新页面不应取消服务端任务，但没有充分分析显式停止、配置策略或 resume 兼容。
- 0-7 分：把问题理解为普通网络断线、普通 loading 状态或简单 stop API。

## 技术手段具体性：20 分

- 17-20 分：提出活动 turn 跟踪、本地清理语义、显式取消入口、可配置 abort 策略、取消帧协议、active id 保留、工具 continuation、observed turn 和 resume 兼容等具体机制。
- 10-16 分：提出取消策略和显式停止，但缺少活动 id、工具 continuation、resume 或请求状态清理细节。
- 0-9 分：只说“abort 时不取消服务端”或“加一个 stop 方法”，缺少可实施机制。

## 必要技术特征完整性：10 分

- 8-10 分：覆盖发送、abort、stream cancel、显式 stop、resume、工具 continuation、多标签观察和配置策略。
- 4-7 分：覆盖主要发送和停止路径，但缺少恢复或 continuation。
- 0-3 分：只覆盖单个按钮或单个 abort handler。

## 问题-手段-效果闭环：15 分

- 13-15 分：能说明各技术手段如何避免误杀 durable turn、保留恢复能力、支持明确取消并减少资源浪费。
- 8-12 分：有基本闭环，但效果主要停留在“刷新后还能继续”。
- 0-7 分：缺少因果关系或技术效果空泛。

## 需求约束遵守：10 分

- 8-10 分：保持现有 resumable streaming、WebSocket protocol、工具 continuation 和 `useAgentChat` API 兼容。
- 4-7 分：基本兼容，但可能破坏默认 resume 或让 explicit stop 语义不清。
- 0-3 分：要求所有 abort 都取消服务端，或所有 stop 都只做本地清理。

## 可实施性：10 分

- 8-10 分：给出清晰的状态变量、事件处理、取消消息、清理时机和配置入口。
- 4-7 分：总体可行，但缺少关键状态或边界条件。
- 0-3 分：方案过于抽象，无法指导工程实现。

## 专利化价值：5 分

- 4-5 分：能抽象为“面向 durable agent chat 的客户端清理与服务端取消解耦控制机制”。
- 2-3 分：有一定机制抽象，但偏普通取消策略。
- 0-1 分：只是 UI 停止按钮或网络重连说明。

## 明确扣分项

- 把浏览器刷新、组件卸载等普通 abort 一律映射为服务端取消。
- 只关闭本地 stream，却没有任何显式服务端取消能力。
- 没有活动 turn id 跟踪。
- 没有考虑工具 continuation 或恢复中的服务端 turn。
- 没有配置策略，无法支持 request-lifetime 型应用。
- 显式取消后错误处理 active request id，导致残余服务端消息污染 UI。

## 源码级评分补充

- `cancelOnClientAbort` 是参考中的配置名，不限定具体命名。只要方案明确区分 durable 默认模式和 request-lifetime opt-in 模式，并描述配置入口与行为矩阵，应按等价机制给分。
- 高分方案必须指出当前 transport 中 caller abort、stream.cancel、本地清理和服务端 cancel frame 易被耦合，并给出拆分后的状态机。
- 只说“刷新不取消服务端”或“增加 stop 按钮”，但没有 active request id 保留、late chunk/done 过滤、resume fallback、tool continuation 和多标签观察路径的方案应显著扣分。

## 档位锚点

- 90-100 分强答案：必须同时具备 durable 默认与 request-lifetime opt-in 行为矩阵、active server turn id 保留、本地 detach 与服务端 cancel 解耦、显式 cancel frame/API、显式取消后的 request id 保留与 late chunk/done 过滤、resume fallback、tool continuation、多标签/observed turn 处理、配置入口和源码级 WebSocket/useAgentChat 接入说明。
- 75-85 分可用但不完整：能正确区分刷新/本地关闭与显式服务端取消，并有 active id 或配置策略，但 resume、tool continuation、late event 过滤、多标签观察或显式取消后的客户端状态清理中缺一到两个关键边界。
- 60-74 分弱答案：只提出“刷新不取消服务端”或“增加 stop”，缺 active id、显式取消协议或恢复/continuation 语义，容易污染 UI 或误杀 durable turn。
- 0-59 分不合格：把普通 abort/close 一律映射为服务端取消、所有 stop 都只本地关闭、或依赖源码中不存在的自动取消能力作为核心机制。

## 分数上限规则

- 如果方案没有服务端活动 turn id 或等价 active request 标识的保留与校验机制，总分通常不得高于 82 分。
- 如果方案没有区分普通客户端 abort、本地 stream cancel、显式服务端取消和 request-lifetime opt-in 行为矩阵，总分通常不得高于 80 分。
- 如果方案没有显式取消帧/取消 API，或显式取消后不能通过 request id 保留/过滤避免迟到消息污染当前 UI，总分通常不得高于 82 分。
- 如果方案没有 late chunk/done 过滤、resume fallback、tool continuation 或多标签观察中的至少两个边界处理，总分通常不得高于 85 分。
- 如果同时缺失 active id、显式服务端取消、配置行为矩阵、resume/continuation 边界处理中任意两个关键机制，总分不得高于 78 分；缺失三个或更多时，总分不得高于 72 分。
- 如果 unsupported_claims 中包含“现有 WebSocket close、AbortSignal、ReadableStream.cancel 或本地 stream close 已经自动取消服务端 durable turn”这类核心源码事实错误，总分必须不高于 75 分；该上限不能因方案总体方向正确而豁免。
- 如果上述核心源码事实错误同时伴随缺少 active server turn id 或 late chunk/done 过滤，总分必须不高于 72 分。
