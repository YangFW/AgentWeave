---
id: context_parameter_guard
name: 多轮任务参数校验 Skill
description: 在多轮对话中补全项目编号、时间范围、阈值、输出格式和目标对象，并在调用工具前检查必填参数。
category: workflow
version: 1.0.0
required_mcps: report,spreadsheet
---

# 多轮任务参数校验

## 触发条件

当用户使用“这个项目”“按刚才的范围”“阈值改成”“把它导出”等上下文引用时使用。

## 执行流程

1. 读取当前独立问题和结构化意图参数。
2. 按 `references/parameter-rules.md` 检查项目、对象、时间、阈值和格式。
3. 缺少必填参数时只询问缺少项，不重复询问已经明确的信息。
4. 调用 MCP 前使用 `scripts/validate_params.py` 的同等规则核验参数。
5. 输出中区分继承参数、当前修改和默认值。

## 输出要求

先简短确认已理解的完整任务，再执行工具调用。
