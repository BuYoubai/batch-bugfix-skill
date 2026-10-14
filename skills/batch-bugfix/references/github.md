# GitHub 发布与恢复

使用 `scripts/github_publish.py`，需要对目标 host 完成认证的 GitHub CLI `gh`。支持创建 Issue 或同仓库 PR；fork、附件、推送、编辑、关闭与合并由项目既有工具在相应授权内完成。创建前检查相似问题和既有 PR，冻结输入。默认 plan 会联网只读检查，不创建对象或写 journal。

## 输入

复制 [Issue 示例](../assets/github-issue.example.json) 或 [PR 示例](../assets/github-pr.example.json) 到目标项目，替换所有示例身份、正文及 SHA。用 `gh api repos/OWNER/REPO --jq '{id,full_name}'` 读取仓库身份，用 `gh api repos/OWNER/REPO/branches/main --jq .commit.sha` 读取分支提交；含斜杠的分支须按 URL 路径参数编码。

| 字段 | 契约 |
|---|---|
| version / kind | `1`；`issue` 或 `pr` |
| host | `github.com` 或企业实例主机名，无协议、端口或凭据 |
| repository / repository_id | 预期的 `owner/name` 与正数字 ID，二者共同校验；名称大小写不敏感 |
| key | 非敏感稳定键，1–128 位字母、数字、下划线、点、连字符 |
| title / body | 已审阅的完整文本，非空，保留换行 |
| head_branch / base_branch | PR 同仓库源/目标分支，必须不同 |
| head_sha / base_sha | PR 两分支的完整小写 SHA |
| draft | PR 可选，布尔值，默认 false；恢复时须与远端一致 |

不接受未定义字段，避免把未支持的标签、fork 或合并选项误当作已执行。发布器在正文末追加隐藏标记 `<!-- batch-bugfix:KEY -->`，输入不要预填。Issue 列表中的 PR 会被排除；PR 通过稳定键及同仓库已打开源分支查重。已关闭对象命中同一键时保留并阻断；无关已关闭 PR 不阻止分支复用。

## 执行

以下路径仅示意：脚本换成实际安装位置，输入及 journal 使用目标项目路径，journal 父目录须已存在。

```text
python /installed/batch-bugfix/scripts/github_publish.py /project/publication.json --journal /project/publication-state.json
```

目标、正文和外部写入已获授权后，同一命令加 `--apply`。创建前再次核对仓库及两分支 SHA；回读核对编号、URL、标题、正文、状态、PR 两端仓库/分支/SHA 及 draft，再检查分支是否移动。`verified` 仅证明本次创建/恢复的回读契约满足，不证明 CI、合并条件或线上验收。

在 Codex 中成功创建或接回本任务所处理的 PR 后，使用平台提供的附件工具关联该 PR；发布脚本本身不依赖平台工具。

## 中断恢复

同一 `(host, repository_id, kind, key)` 使用同一份输入、journal 和一个协调者。锁只保护同一 journal，不提供跨机器的全局幂等；应避免多个协调者同时发布同一对象。

创建前原子写入 `pending`。POST 结果不明时写 `uncertain` 并只读定位同键对象；找到后完整回读，否则停止。再次执行相同输入和 journal 只做核对，不重复 POST。更改输入、丢弃 journal 或换 key 前须查明原请求。无参数预览不更新 journal。

现有对象被修改、关闭、移动分支或移除标记时，不覆盖现状。锁残留时先核对记录的进程确实结束，再清理该精确锁文件。日志保留非敏感错误类别；认证、权限或策略错误由 CLI 既有诊断处理，原始响应不写入交付材料。

## 依据

- [gh api：JSON 标准输入与 host](https://cli.github.com/manual/gh_api)
- [Issues：列表包含 PR](https://docs.github.com/en/rest/issues/issues#list-repository-issues)
- [Pull requests：创建及读取](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request)
- [Branches：读取分支提交](https://docs.github.com/en/rest/branches/branches#get-a-branch)
