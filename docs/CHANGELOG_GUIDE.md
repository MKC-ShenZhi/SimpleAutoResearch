# 更新记录怎么维护 / Changelog guidelines

Changelog 给用户看变化，不是提交列表、验收日志或内部施工计划。
Keep the changelog user-facing, not a commit dump, test log or implementation queue.

## 日期和状态 / Dates and status

- 使用 `## YYYY-MM-DD`，按日期倒序；同日修改合并，不为每个补丁加一条。
- 同步维护根目录的中英文记录。跨天工作无法准确拆分时，在确认日注明汇总范围，不编造每天的成绩。
- 未合入 main 的内容明确写“开发中／未发布”，实现、工程验证和真实任务验收分开描述。
- 更新已有日期条目，不把所有进展不断堆到一个“未发布”段落。

Use reverse-chronological dates and one entry per day. Keep Chinese and English aligned. Label development-branch work explicitly. Do not invent daily history or equate implementation with acceptance.

## 每日模板 / Daily template

```markdown
## YYYY-MM-DD — 可选主题（开发中内容注明状态）

### 新增 / Added

- 用户现在可以做什么，得到什么。

### 优化 / Improved

- 原来的什么操作变得更简单、更可靠或更省成本。

### 修复 / Fixed

- 什么可观察的问题得到修复；必要时说明兼容边界。

### 尚未完成 / Not yet complete

- 影响用户使用或当前发布判断的重要缺口。
```

空分类删掉。每个分类通常1–3条，每条一个主题，优先一句话；进行中的修复标为“正在修复”，不要提前放入已修复。没有有意义变化的日期不用凑条目。

Omit empty sections. Prefer one sentence per item and 1–3 items per category. Mark unfinished fixes as in progress. No entry is needed for a day without meaningful changes.

## 发布前核对 / Before updating

1. 对照提交、实际代码和验收记录，确认行为及所属分支。
2. 写用户收益，少写内部类名、合同代号和测试细节。
3. 案例注明框架生成还是人工编辑；复跑不是重新自动生成。
4. “减少成本”“更稳定”“完整完成”等结论必须有对应证据；否则只写具体改变。
5. 费用、失败轨迹、检查日志放案例来源或交接中。周报另写成果、缺口和下一步，不复制整段 Changelog。

Verify behavior and branch against code and records. Describe user impact. Disclose artifact provenance and distinguish replay from generation. Evidence is required for quality or efficiency claims. Keep detailed costs, failures and test logs in provenance or handoff records.
