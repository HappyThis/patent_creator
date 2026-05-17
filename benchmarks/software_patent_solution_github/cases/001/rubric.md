# 评分标准

总分 100 分。

## 环境阅读与需求理解：15 分

- 13-15 分：能结合需求并在当前项目环境中识别 Mission Control 既有多 runtime 管理、session API、transcript、continue、onboarding/settings、chat workspace 和 terminal view 等相关上下文。
- 8-12 分：能识别 OpenCode 会话接入目标，也能阅读部分项目路径，但对既有 runtime 兼容、继续能力或误导性入口控制分析不足。
- 0-7 分：未有效阅读当前项目环境，只停留在“增加 OpenCode 选项”或“新增一个接口”的层面。

## 技术问题识别：15 分

- 13-15 分：准确指出问题本质是新增本地 agent runtime 时的状态来源差异、会话归一化、能力边界声明和既有 runtime 兼容。
- 8-12 分：能指出需要支持 OpenCode 会话，但未充分分析本地状态、继续能力和终端能力差异。
- 0-7 分：把问题理解为普通 UI 适配、配置项增加或第三方命令调用。

## 技术手段具体性：20 分

- 17-20 分：提出运行时类型注册、本地状态发现、状态结构适配、会话对象归一化、transcript 解析、continue 路由、能力 gating、契约同步和可复现验证等具体机制。
- 10-16 分：提出 runtime adapter 或 session adapter，但关键流程、数据边界或能力控制不完整。
- 0-9 分：只说“接入 OpenCode API”“读取会话”“增加按钮”，缺少可实施机制。

## 必要技术特征完整性：10 分

- 8-10 分：覆盖运行时发现、会话列表、会话详情、继续操作、UI 展示、不可用能力控制和测试验证。
- 4-7 分：覆盖主要后端或前端路径，但缺少能力边界或验证机制。
- 0-3 分：只覆盖单个接口或单个页面。

## 问题-手段-效果闭环：15 分

- 13-15 分：能说明各技术手段如何解决本地状态差异、会话归一化、错误能力承诺和既有流程兼容问题，并形成可验证效果。
- 8-12 分：有问题和手段，但效果主要停留在“支持 OpenCode”。
- 0-7 分：缺少因果闭环或效果空泛。

## 需求约束遵守：10 分

- 8-10 分：不破坏 Claude、Codex、Hermes 和 gateway 会话；不错误开放 PTY；不依赖开发者本机环境；保持 API 文档和测试一致。
- 4-7 分：基本兼容现有路径，但存在不明确的能力承诺或环境依赖。
- 0-3 分：要求重写会话系统、删除既有 runtime、强依赖外部 gateway 或假设 OpenCode 永远可用。

## 可实施性：10 分

- 8-10 分：给出清晰模块划分、流程、输入输出和失败处理，工程上可以按方案实现。
- 4-7 分：总体方向可行，但缺少关键数据结构、接口边界或错误处理。
- 0-3 分：方案过于抽象，无法指导实现。

## 专利化价值：5 分

- 4-5 分：能抽象为“异构本地 agent runtime 的状态适配、会话归一化和能力边界控制机制”。
- 2-3 分：有一定专利化表达，但偏普通集成方案。
- 0-1 分：只是第三方工具接入说明，缺少技术构思。

## 明确扣分项

- 直接把 OpenCode 伪装成 Claude、Codex 或 gateway session。
- 只在 UI 中增加 OpenCode 选项，不解决会话状态读取和能力边界。
- 假设所有 OpenCode 会话都可继续、可终端附着或可成功执行。
- 要求用户手动导入每个会话，缺少自动发现或适配机制。
- 为了接入 OpenCode 重写整个 session 系统，破坏既有 runtime。
- 缺少测试夹具或可复现验证路径，依赖开发者机器的真实 OpenCode 状态。

## 源码级评分补充

- 允许方案把 OpenCode 真实存储格式作为待适配外部事实处理；只要提出清晰的 adapter 合约、fixture 和命令替身验证，不应因未猜中具体 OpenCode 目录结构而扣重分。
- 高分方案必须说明如何接入现有 session 聚合、transcript、continue、runtime setup/settings、conversation list 和 terminal capability gating，而不是新增旁路页面。
- 若方案声称 OpenCode 全量支持 continue、PTY attach 或 provider/model 状态，但没有能力检测和失败返回，按“误导性能力承诺”扣分。

## 档位锚点

- 90-100 分强答案：必须同时具备 runtime 注册、可配置本地状态发现、session list 归一化、transcript 解析与占位降级、continue 路由和失败分类、conversation list/chat workspace 集成、runtime setup/settings、PTY/terminal capability gating、OpenAPI/API 契约同步、fixture 与命令替身验证，并且不破坏 Claude/Codex/Hermes/OpenClaw 既有路径。
- 75-85 分可用但不完整：能把 OpenCode 作为异构 runtime 接入现有 session 聚合，方向兼容现有架构，但 transcript、continue 失败分类、PTY gating、API 契约或可复现测试中缺一到两个关键工程边界。
- 60-74 分弱答案：只做 session list 或 UI/runtime 选项，缺 transcript 或 continue 路由，能力声明容易误导用户，验证依赖真实开发机状态。
- 0-59 分不合格：把 OpenCode 伪装成现有 runtime、要求重写 session 系统、假设所有能力都可用，或只给第三方命令调用说明。

## 分数上限规则

- 如果方案没有 transcript API/parser 接入和消息归一化，总分通常不得高于 82 分。
- 如果方案没有 continue 路由、能力检测和 binary/provider/model/session/resume unsupported 等失败分类，总分通常不得高于 82 分。
- 如果方案没有 terminal/PTY attach capability gating，可能把 OpenCode 误展示为可终端附着，总分通常不得高于 84 分。
- 如果方案没有 OpenAPI/API index/schema 契约同步和前端能力字段传播，总分通常不得高于 84 分。
- 如果方案没有 OpenCode fixture、binary stub、session list/transcript/continue/PTY unsupported 的可复现测试，总分通常不得高于 80 分。
- 如果同时缺失 transcript、continue 失败分类、PTY gating、API 契约、测试夹具中任意两个关键机制，总分不得高于 78 分；缺失三个或更多时，总分不得高于 72 分。
