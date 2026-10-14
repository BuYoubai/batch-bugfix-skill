# GitLab 发布与恢复

使用 `scripts/gitlab_publish.py`，需要对目标主机完成认证的 `glab`。支持创建 Issue 或同项目 MR，保留原有 version 1 输入。

| 字段 | 契约 |
|---|---|
| version / kind | `1`；`issue` 或 `mr` |
| host | 主机名，可带端口，无协议或凭据 |
| project_id / project_path | 正数字 ID 与预期的 `namespace/project` |
| key | 非敏感稳定键，字母、数字、下划线、点、连字符 |
| title / description | 已审阅、非空的完整文本 |
| source_branch / target_branch | MR 源/目标分支 |
| source_sha / target_sha | MR 两分支完整小写 SHA |

复制 [Issue 示例](../assets/gitlab-issue.example.json) 到项目后替换示例值；MR 添加表中的分支与 SHA 字段。创建前单独检查相似问题。脚本以本技能目录为根调用，或使用安装位置的绝对路径；输入与 journal 位于项目。

```text
python scripts/gitlab_publish.py publication.json --journal publication-state.json
```

默认 plan 会读取远端，不创建对象、不写 journal。目标、正文与远端操作已获授权后加 `--apply`。脚本不会 push、上传附件、编辑、关闭、合并或部署。

正文追加稳定隐藏键，创建前核对项目及 MR 两分支 SHA，创建后回读标题、正文、项目、状态和 MR 分支/SHA。结果不明时使用完全相同的输入与 journal 重跑，只核对已发生结果，不再次 POST。现有对象内容不同、已关闭或重复命中时停止，不覆盖或重建。

每个 `(host, project_id, kind, key)` 保持一份输入/journal 与一个协调者。锁只覆盖该 journal，不是跨机器幂等保证；残锁须先核对原进程已结束。`verified` 不证明 CI、合并或真实业务验收。
