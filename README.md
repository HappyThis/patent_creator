<h1 align="center">Patent Creator</h1>

<p align="center">
  <a href="https://github.com/HappyThis/patent_creator/releases"><img alt="release" src="https://img.shields.io/github/v/release/HappyThis/patent_creator?label=release"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776AB">
  <img alt="backend" src="https://img.shields.io/badge/backend-FastAPI-009688">
  <img alt="frontend" src="https://img.shields.io/badge/frontend-React%20%2B%20Vite-646CFF">
</p>

![Patent Creator 封面](docs/assets/readme-cover.png)

Patent Creator 是一个面向专利交底书写作的 AI Agent 工作台：从项目资料和对话生成可预览、可修订、可导出的 DOCX 文档。

它不是普通聊天机器人，也不是空白文档编辑器。用户通过对话说明目标，Agent 负责阅读材料、规划章节、调用工具、写入正文和持续修订；用户主要判断方向、补充事实和提出修改意见。

## 为什么需要

专利交底书写作经常不是从完整文档开始，而是从分散材料开始：

- 项目代码、研发资料、产品说明里有技术细节，但缺少专利表达。
- 发明人知道做了什么，但很难组织成背景技术、技术问题、技术方案和实施方式。
- 多轮修改容易散落在聊天记录里，最终文档和对话上下文脱节。
- 公式、附图、章节结构和 DOCX 导出需要保持一致，不能只停留在纯文本草稿。

Patent Creator 关注的是中间地带：把想法、资料和对话推进成结构化交底书。

## 快速开始

复制配置文件：

```bash
cp env.example .env
```

填写 `.env` 中的模型配置，至少需要：

```text
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=gpt-5.5
```

先启动 Docker Desktop，再启动本地开发环境：

```bash
./scripts/start-dev.sh
```

Windows PowerShell：

```powershell
.\scripts\start-dev.ps1
```

默认访问：

```text
http://127.0.0.1:5173
```

附图编辑与导出默认使用本机自托管 Draw.io：

```text
PATENT_CREATOR_DRAWIO_EMBED_URL=http://127.0.0.1:8081/
```

启动脚本会通过 `compose.drawio.yaml` 自动拉起并等待 Draw.io；首次启动会下载镜像，后续启动会直接复用容器。脚本退出后 Draw.io 容器仍会保留，可按需停止：

```bash
docker compose -f compose.drawio.yaml down
```

系统会自动补充 `offline=1`、`embed=1` 和 `proto=json` 等参数。配置为其他地址时，启动脚本会跳过本地容器；非本机 Draw.io 地址默认拒绝，确需使用受信任的内网或公网服务时，必须显式设置：

```text
PATENT_CREATOR_ALLOW_NONLOCAL_DRAWIO=true
```

运行数据默认保存在：

```text
~/.patent_creator
```

## 核心能力

- 对话驱动写作：用户用 chat 指挥 Agent，不需要在复杂表单里拆任务。
- 项目与会话管理：侧边栏管理项目、会话和运行状态。
- Agent 过程记录：展示工具调用、上下文压缩、内容增强和文本输出等过程。
- 交底书预览：右侧预览当前交底书正文，便于持续检查。
- 章节级编辑：Agent 可以围绕章节写入、补充、替换和重排内容。
- 公式与附图：支持块级公式、行内公式、Draw.io 可编辑附图和正文引用。
- DOCX 导出：从当前交底书状态导出 Word 文档，而不是从聊天记录拼接文本。
- Benchmark 反馈：用固定案例、重复运行和评估结果反推写作链路质量。

## Benchmark

Benchmark 用来衡量 Agent 是否能从代码、资料或描述中形成稳定、准确、有专利价值的技术方案。评估重点不只是生成字数，而是是否识别真实技术问题、抓住关键创新机制、形成可实施且可保护的技术方案。

| Benchmark | 结果 ID | 规模 | 平均分 | 通过情况 | 备注 |
| --- | --- | --- | --- | --- | --- |
| 专利技术方案 benchmark | `20260619-gpt55-baseline-10cases-5x-w10` | 10 cases x 5 repeats | 93.14 / 100 | 50/50 scored，artifact_success=50/50 | subject=`gpt-5.5`，judge=`codex`，workers=10 |

## Roadmap

- 增强复杂图片生成和附图管理。
- 改进 DOCX 导出的视觉校准和更多真实样本文档验证。
- 加强网页资料读取、引用管理和来源追踪。
- 增强专利语言风格控制和章节结构约束。
- 扩展 benchmark 覆盖范围，持续验证多轮写作稳定性。

## 技术栈

- Backend：Python 3.11+、FastAPI、OpenAI SDK、python-docx。
- Frontend：React、Vite、TypeScript、KaTeX、Draw.io。
- Dev workflow：`scripts/start-dev.sh` 和 `scripts/start-dev.ps1` 一键启动本地开发环境。

## 开源许可

本项目基于 [MIT License](LICENSE) 开源。
