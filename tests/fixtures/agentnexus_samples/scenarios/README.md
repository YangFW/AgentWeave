# AgentNexus 场景样例

这组文件是 `docs/TEST_PLAN.md` 的场景输入，不是生产数据，也不包含真实密钥。它们和同级的 `sample.*` 格式样例配合使用：

| 文件 | 对应用例 | 用法 |
| --- | --- | --- |
| `weather-travel-brief.md` | N-01、N-02 | 天气多轮续问后切换到杭州—宁波自驾计划，并生成 DOCX/PPTX |
| `transformer-learning.md` | N-04、N-05 | 作为结构参考，验证 Markdown 渲染和后续结构复用 |
| `codex-alternative-prd.md` | N-04 round2 | 在不继承 Transformer 事实的前提下复用章节结构 |
| `meeting-notes.md` | Skill-01 | 命中会议纪要 Skill，检查摘要、决策、风险和行动项 |
| `knowledge-base-brief.md` | KB-01 | 建立知识库后检索唯一标识和验收规则 |
| `expert-review-brief.md` | E-01、E-02 | 专家团从产品、技术、安全三个角度评审并汇总 |
| `artifact-request.md` | 产物专项 | 用同一输入分别生成 MD、HTML、DOCX、XLSX、PDF 和 PPTX |

## 推荐使用顺序

1. 普通模式先上传 `weather-travel-brief.md`，发送“今天天气怎么样？”，按平台提示补充“宁波”，再发送“明天呢？”。
2. 在同一个对话继续发送“根据附件写明天杭州自驾去宁波的旅行计划，并生成 Word 和 PPT”。
3. 新建对话上传 `transformer-learning.md`，要求生成 Markdown；下一轮上传或引用 `codex-alternative-prd.md`，要求保持前一份文档的结构。
4. 安装并启用同级的 `sample_skill.zip`，上传 `meeting-notes.md`，发送“整理这份会议记录”。
5. 将 `knowledge-base-brief.md` 建立索引后，检索 `ORBIT-7391`、`P0` 和“最终验收”。
6. 专家模式上传 `expert-review-brief.md`，要求产品、技术、安全专家分别评审并给出统一结论。
7. 上传 `artifact-request.md`，按格式逐项生成并预览可下载产物。

## 验证要点

- 回答必须引用附件中的事实，例如 `宁波东钱湖`、`ORBIT-7391`、`P0`，不能凭空改成天气或其他任务。
- 过程面板应显示当前目标、动态计划、对应 Skill/MCP 子节点、模型、最终验收和产物状态。
- 生成文件只有在验收通过后才出现在下载区；PPTX 未配置时应给出能力提示，不能生成损坏文件。
