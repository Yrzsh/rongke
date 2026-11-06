# Tavotto 与 Supervisor 集成记录

## 固定来源

| 项目 | 官方来源 | 固定 revision | 本地落点 |
|---|---|---|---|
| Tavotto | https://github.com/Tavotto/Tavotto | `da2b2b7b26a9b5b23f7c661028f13577423e10b5` | `vendor/Tavotto/` |
| Supervisor.skill | https://github.com/UniversePeak/Supervisor.skill | `46c7cd8ae0e0cc07a8cdbc2ef729cced558269b7` | `vendor/Supervisor.skill/` |
| Supervisor-Skills | https://github.com/HKUSTDial/Supervisor-Skills | `a20d76ef3979b804d7dbf599d577826957e0a8a9` | 原有 `vendor/skills/`，未重复引入 |

复制前已获得用户对复制及许可处理的明确决定。各仓库LICENSE原样保留，`.git`、缓存及本机配置不作为产品文件。

## 实际使用方式

- `stages.py` 直接发现 `tavotto-figure` 与 `create-supervisor`。后者是单技能库根，真实技能名不是目录名。
- Tavotto交接使用官方 `codex-plugin/skills/tavotto-figure/scripts/handoff.py <script> --run never`。发现链、桌面版与CLI区别、错误码均归官方脚本，不重新实现。
- 荣珂只从既有产出物围栏读取用户选中的图与同目录同stem脚本，复制至任务内 `tavotto/<stem>-<时间>-<pid>/`，再交给官方工具。原脚本不执行、原图和旧报告不覆盖。
- Tavotto的 `launch_success`、`render_success`、`verified_success` 分开呈现。未确认渲染不等于失败，更不等于成功。
- 精修导出仍须调用荣珂 `FigureAuditor`，使用原始图契约，生成 `figure-qa-tavotto.json`。没有契约或独立PDF就不审，不默认“通过”。
- 导师课题阶段是人工选择的工作上下文，不是研究完成度；不自动执行模型任务或替换Supervisor已有技能。

## 已知边界

- 复制源码不等于安装Tavotto。可用性由官方发现链决定；本机未找到CLI时图页明确显示，不影响原绘图。
- PDF/PNG本身不能恢复参数化编辑；缺同stem脚本时拒绝，不拿位图缩放冒充矢量编辑。
- 官方交接可能在副本目录写注册表、运行或生成文件，但不会收到原脚本路径。
- 不内嵌另一套画布，不自动安装依赖，不自动上传诊断或开启遥测。
