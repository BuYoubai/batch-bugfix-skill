# Batch Bugfix

把一批问题推进到明确阶段的 Agent Skill：确认可达的缺陷，保留复现与正常对照，按完整提交链审查，在隔离环境比较具名回归，再按授权交付到 GitHub 或 GitLab。

适合“检查这些模块，修复确认的问题，审查验证后停在推送前”或“把这批已审补丁整理成 Issues/PR”。单个明确 bug 可沿用项目已有诊断流程。

## 能做什么

- 从候选、复现、补丁、集成或中断发布阶段继续，保留每项证据、决策和交付状态。
- 检查完整线性提交链、分支头与触及文件，比较测试身份及失败原因，识别用例消失、跳过和回归。
- 通过已认证的 `gh` 创建同仓库 GitHub Issue/PR，或通过 `glab` 创建 GitLab Issue/同项目 MR。
- 默认联网只读预览；显式执行后回读身份、正文、状态及分支/SHA。请求结果不明时核对既有对象，不盲目重建。

分支推送、合并和部署遵守各自授权及项目规则。发布脚本只创建并核对对象；分支需先由已获授权的项目工具推送。GitHub fork PR 暂由项目既有工具处理。

## 安装

把 [skills/batch-bugfix](skills/batch-bugfix/SKILL.md) **整个目录**安装到所用 Agent 的 Skill 目录，保留 `scripts/`、`references/`、`assets/` 和 `tests/` 的相对位置。只复制 `SKILL.md` 会丢失必需资源。安装位置与刷新方式以所用 Agent 的配置为准。

下面的跨平台命令从仓库根目录执行。将目标路径替换成实际的、尚不存在的技能目录；`copytree` 遇到同名目录会停止，便于先比较已有安装。

```text
python -c "import shutil; shutil.copytree('skills/batch-bugfix', '/YOUR_AGENT_SKILLS_DIR/batch-bugfix')"
```

Windows 目标路径可写成 `D:/agent/skills/batch-bugfix`。安装后按平台方式刷新并显式调用 `$batch-bugfix`，或让支持自动发现的平台按任务描述选择。Skill 文件可移植，具体平台的发现与委派能力仍需实际验证。

如果已有本地定制版，先安装到隔离的项目/测试位置比较，不覆盖原入口。这个仓库不修改 Agent 全局配置，也不携带个人控制面注册器。

## 依赖与可选增强

| 用途 | 依赖 |
|---|---|
| 读取技能、使用模板 | 支持 Skill 的 Agent 与目标项目 |
| 脚本、回归测试 | Python 3.10+ 标准库；Git 清单及夹具需要 `git` |
| GitHub 创建/回读 | 对目标 host 完成认证的 GitHub CLI `gh` |
| GitLab 创建/回读 | 对目标 host 完成认证的 GitLab CLI `glab` |

`diagnosing-bugs`、`implement`、`tdd`、`code-review`、`pr` 等是可选增强；未安装时按技能内的最小契约和项目流程执行。项目明确指定的审查或交付要求仍优先。外部技能不随本仓库打包、不自动安装。

模型默认沿用用户与环境选择；可复制 [角色配置示例](skills/batch-bugfix/assets/model-routing.example.json) 到项目 `.batch-bugfix/model-routing.json` 后填写可用模型。配置供 Agent 阅读，不修改全局路由。

## 使用

```text
用 $batch-bugfix 检查当前上游的文件上传模块。
只修复支持输入下可复现的问题，保留具名失败→通过与正常对照。
完成审查和本地回归后停在推送前。
```

准备发布时，按目标读取 [GitHub](skills/batch-bugfix/references/github.md) 或 [GitLab](skills/batch-bugfix/references/gitlab.md)；所有输入示例都是合成材料，必须替换仓库身份、正文和 SHA。以下命令从仓库根目录执行，示例项目目录需已存在：

```text
python skills/batch-bugfix/scripts/github_publish.py /PROJECT/publication.json --journal /PROJECT/publication-state.json
```

默认只读检查。确认目标和内容已获远端写入授权后再加 `--apply`。每个发布对象使用固定输入、稳定 key、同一份 journal 与一个协调者。文件锁不提供跨机器的全局幂等；发布前仍须检查语义相近的远端对象。

## 验证

从仓库根目录执行：

```text
python -B tools/check_package.py
python -B -m unittest discover -s skills/batch-bugfix/tests -v
```

检查使用临时 Git 仓库和模拟 API，无需账号、凭据或真实网络。CI 配置在 Windows/Linux、Python 3.10/3.13 上运行这些检查。工作流文件存在不代表远端已运行；真实 GitHub/GitLab 写入和各 Agent 的发现能力须分别验收。

## 目录与来源

- `skills/batch-bugfix/` 是可安装单元；`tools/` 和 `.github/` 用于维护仓库。
- 核心来自既有 `batch-bugfix` 工作流，公开版增加 GitHub 适配并将本机模型/平台偏好改为可选配置；问题准入、具名回归和完整提交链契约保留。
- 可选 `pr` 技能来源为 [mattpocock/skills](https://github.com/mattpocock/skills)；本仓库未复制其正文，使用时遵守其来源与许可。

代码与文档按 [MIT License](LICENSE) 提供。
