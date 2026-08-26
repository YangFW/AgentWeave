# AgentNexus 平台测试样例

这是一套与 `docs/TEST_PLAN.md` 配套的可上传样例，主题统一为“AgentNexus 平台测试资料”。样例不包含真实 API Key、个人信息或生产数据。

## 文件清单

| 文件 | 用途 | 建议测试 |
| --- | --- | --- |
| `sample.txt` | 纯文本解析 | 上传后总结关键结论、风险和待办 |
| `sample.md` | Markdown 结构 | 检查标题/表格渲染，以及后续文档结构复用 |
| `sample.csv` | CSV 表格 | 导入后生成汇总或 Excel |
| `sample.json` | 结构化数据 | 检查 JSON 解析、字段引用和能力清单 |
| `sample.docx` | Word 文档 | 检查段落、列表、表格和中文文本解析 |
| `sample.xlsx` | Excel 工作簿 | 检查多工作表、公式、渲染和下载 |
| `sample.pptx` | PowerPoint | 检查多页文本、版式和下载 |
| `sample.pdf` | 可搜索 PDF | 检查 PDF 文本提取、表格和浏览器预览 |
| `sample.html` | HTML 页面 | 检查安全预览、列表、表格和链接 |
| `sample_skill.zip` | Skill 安装包 | 在技能中心上传安装；安装后启用并匹配“会议纪要”任务 |
| `sample_skill/SKILL.md` | Skill 原文件 | 需要查看或手动创建 Skill 时使用 |
| `mcp-readonly.json` | 本地 stdio MCP 配置样例 | 导入配置；确认服务默认停用、只读标记和权限提示 |
| `mcp-missing-parameter.json` | 缺少必填参数的 MCP 配置 | 导入后模拟调用，确认提示“缺少 project_id”而不是展示堆栈 |

## 建议的首轮操作

1. 在同一个普通模式对话中上传 `sample.txt`、`sample.md`、`sample.json` 和 `sample.pdf`。
2. 发送：`请总结我上传的测试资料，列出关键结论、风险和待办。`
3. 继续发送：`保留上面的章节结构，整理成 Word 和 Markdown，并提供下载。`
4. 再发送：`生成一个 Excel 汇总和 3 页 PPT，并提供下载。`
5. 在技能中心上传 `sample_skill.zip`，启用后发送：`把这份会议记录整理成摘要、决策、风险和行动项。`
6. 在工具接入导入 `mcp-readonly.json`；再导入 `mcp-missing-parameter.json`，验证参数缺失提示和错误卡片。

## 预期检查点

- 附件进入当前对话上下文；新对话不会混入旧对话正文。
- 计划、大节点和 Skill/MCP 子节点能实时显示，并可下钻查看工具和参数。
- Markdown 预览按格式渲染，下载的 `.md` 仍保留原始 Markdown。
- DOCX/XLSX/PPTX/PDF 可预览、可下载，重新打开任务后仍能访问。
- 模型、网络、权限或工具失败时，对话区显示简短、可读的原因，不展示内部思考、路径、Token 或堆栈。
- 只有通过最终目标确认和产物验收的文件才进入下载区。

## 配置注意

- `mcp-readonly.json` 中的命令只是导入/权限测试占位配置，启用前请替换为你已审核的本地 MCP 命令和授权目录。
- `mcp-missing-parameter.json` 使用 `example.invalid`，故意不会连接成功；它只用于验证 Schema 和友好错误提示。
- 同级的 `agentnexus_samples_build/` 目录是本地生成与渲染的 QA 中间文件，不需要上传到平台。
