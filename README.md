# Skills

个人 AI 编程助手技能库：集中存放可在 Cursor、Codex、Claude Code 等工具间复用的 Skill 文件。

本仓库用于：

- 备份与版本管理各类 Skill
- 通过链接分享技能说明与用法
- 作为跨工具同步时的远端参考源

## 技能列表

| 技能 | 简介 | 链接 |
|------|------|------|
| **syncing-skills-across-tools** | 在多个 AI 编程工具的 skills 目录之间预览并同步同一 Skill（先 dry-run 展示路径与删除计数，经明确批准后再 apply）。 | [目录](https://github.com/zhoumaogen/skills/tree/main/syncing-skills-across-tools) · [SKILL.md](https://github.com/zhoumaogen/skills/blob/main/syncing-skills-across-tools/SKILL.md) |

## syncing-skills-across-tools

在创建、修改或修复某个 Skill，并需要把它镜像到多个工具的 skills 目录时使用。

**核心约定**

- 以当前编辑的 Skill 为唯一事实源
- 未经当前对话中对归一化目标路径与删除摘要的明确批准，不得写入任何目标目录

**快速用法**

```bash
python -B scripts/sync_skills.py --source <skill-dir> --dry-run --json
python -B scripts/sync_skills.py --source <skill-dir> --apply \
  --plan-fingerprint <fingerprint> --approved-target <path> [--approved-target <path> ...]
```

详见：[SKILL.md](https://github.com/zhoumaogen/skills/blob/main/syncing-skills-across-tools/SKILL.md)

## 目录结构

```text
skills/
├── README.md
└── syncing-skills-across-tools/
    ├── SKILL.md
    ├── scripts/
    └── tests/
```
