先给结论：**这 5 个提交是在修"实验可信度"和"进化安全性"，不是已经证明了 GLM 进化有效。仓库里目前仍然没有新的有效实验结果；`results/summary.json` 还是旧结果，且旧结果里 Evolved 明显比 Seed 差。所以下一步不能继续盲目让 GLM 多生成 Skill，必须先跑出一份可信的 sealed evidence。**

## 1. 现在仓库状态：代码修对了一部分，但结果还没出来

这次最新提交 `e75ef54` 的 commit message 说修了三个关键点：`evaluator_sumo.py` 的 throughput 语义、`PhaseCommand` import bug、`is_green_phase` 重复定义。 另外这 5 个提交确实做了几件我之前要求的修复。

第一，`PhaseCommandExecutor` 已经新增，用来统一 runner 和 evaluator 里的 `PhaseCommand` 执行逻辑，特别是避免 switch 时跳过 yellow/all-red。它明确支持 runner 模式和 evaluator 模式，并提供 `apply()` 和 `process_pending_switches()`。 具体 switch 逻辑也改成了：当前为绿色且目标不同，则把当前绿灯剩余时间压到 1 秒，让 SUMO 自然过渡，再记录 pending duration。

第二，`runner.py` 已经在 OnlineController 路径里创建 `PhaseCommandExecutor`，并在每步调用 `_control_with_online_controller()`。 在线控制时先构建所有路口 observation，再处理 pending switch，再对每个路口调用 `controller.step()`，最后通过 executor 下发 command。 这比之前只调 cycle plan 强很多。

第三，`evaluator_sumo.py` 也开始使用同一个 `PhaseCommandExecutor`，这说明 evaluator 和 runner 的执行器开始对齐。 这一步非常关键，因为之前进化时 evaluator 的动作环境和真实 runner 不一致。

第四，seed cohort 的路径已经从 `/home/samuel/...` 这种绝对路径改成了 repo-relative 相对路径，例如 `artifacts/skills/1159176756/cycle/v0000`。 `SkillCohort` 里也新增了 `_resolve_path()`，支持相对路径按 cwd 或项目根目录解析。

第五，MaxPressure 也往正确方向走了：`MaxPressureCanonical` 现在说明自己优先用精确 downstream mapping，其次启发式匹配，最后才回退到全局平均。 但注意，它还保留 fallback，真正强 baseline 仍然需要显式构建 phase-to-downstream mapping。

## 2. 但现在实验大概率还没真正重跑

仓库里的 `results/summary.json` 仍然是旧格式：没有新版 `throughput_per_hour`、`avg_queue_source`、`waiting_time_source`、`controller_stats` 等字段。旧结果里 `SignalClaw-Evolved` 还是很差：`avg_queue=806.01`、`avg_waiting_time=163.61`、`completed_vehicles=7098`；而 `SignalClaw-Seed` 是 `avg_queue=747.61`、`avg_waiting_time=126.89`、`completed_vehicles=7508`；legacy `SignalClaw` 更好，`completed_vehicles=8013`。

`evolution_summary.json` 也还是 replay-score 风格，只记录 `cycle_score`、`phase_score`、`cycle_seed_score`、`cycle_improvement`，没有 `accepted_for_deployment`、`has_real_sumo_report`、`paired_eval_passed` 这些部署证据字段。

所以我现在的判断是：

> **代码已经在修正确闭环；但仓库里还没有看到新闭环跑出的有效进化效果。旧结果不能再拿来证明，也不能拿来否定这 5 个提交。**

## 3. 下一步第一件事：先跑出"是否真的有效"的证据

现在不要再继续写新方法，先做一个 **Evidence Run**。本地应该先干净重跑，而不是沿用旧结果。

建议先跑：

```bash
# 1. 确认在最新 main
git pull
git rev-parse HEAD

# 2. 建议先把旧结果备份，而不是覆盖后混淆
mkdir -p results_archive
cp -r results results_archive/results_before_e75ef54_$(date +%Y%m%d_%H%M%S)

# 3. 重跑基础实验
python -m signalclaw.experiments.runner
```

重跑后先看 `results/summary.json` 是否出现这些字段：

```text
throughput_per_hour
avg_queue_source
waiting_time_source
stops_source
controller_stats
```

如果没有这些字段，说明其实没有用新 runner 跑。新版 `SimulationMetrics.summary()` 本来应该输出 `throughput_per_hour`、`avg_queue_source`、`waiting_time_source`、`stops_source`。

然后必须检查 `SignalClaw-Seed` 和 `SignalClaw-Evolved` 的 `controller_stats`：

```text
cycle_plan_count > 0
phase_command_count > 0
phase_switch_count / hold / extend / shorten 有记录
online_glm_calls = 0
```

runner 里确实会打印这些 controller stats，并强制 `online_glm_calls=0`。 如果这些计数没有出现，那说明双 Skill 闭环仍然没有真正跑起来。

## 4. 第二件事：先别急着"进化"，先确认 Seed artifact 不比 legacy 差太多

旧结果里最大问题不是 Evolved 差，而是 **Seed artifact path 本身就比 legacy SignalClaw 差太多**。如果 Seed artifact 都复现不了 legacy SignalClaw，那么后续 Evolved 和 Seed 的比较基准就是坏的。

下一步必须做一个 `parity_report.csv`，记录同一时刻、同一路口：

```text
sim_time
tls_id
current_phase
legacy_cycle_plan
seed_cycle_plan
seed_phase_command
actual_phase_command
actual_traci_phase
queue_by_phase
waiting_by_phase
```

验收标准：

```text
SignalClaw-Seed artifact 的 completed_vehicles 不应比 legacy SignalClaw 低超过 2%~3%
avg_waiting / avg_queue 不应明显恶化
phase_command_count 必须大于 0
```

如果 Seed artifact 仍然比 legacy 差很多，优先修 artifact seed，而不是修 GLM。否则 GLM 是在坏基准上进化。

## 5. 第三件事：进化必须改成"候选池 + 锦标赛"，不能靠单个 GLM candidate

近期比较可靠的 AI 程序进化方法都有一个共同点：**LLM 只负责生成候选，自动 evaluator 才是裁判**。FunSearch 明确是把 LLM 生成的代码函数和 automated evaluator 结合，自动执行和评分，高分程序再进入下一轮；DeepMind 也强调这是一种 self-improving loop。([Google DeepMind][1]) AlphaEvolve 也是把 Gemini 生成程序和自动 evaluator/evolutionary framework 结合，而不是让大模型自己判断好坏。([Google DeepMind][2])

所以 SignalClaw 的下一版不要再做：

```text
GLM 生成 1~3 个 Python skill
AST 过了
Replay score 好
就叫 evolved
```

要改成：

```text
每个路口、每种 skill：
  GLM 生成 20~50 个 DSL/patch candidate
  AST/schema/feature mask 先筛
  behavior contract 再筛
  replay safety 再筛
  micro-SUMO 600s 快筛
  full-SUMO 3600s paired tournament
  只有通过 non-degradation gate 才能成为 champion
```

也就是说，**进化有效不是要求每个 candidate 都变好，而是要求 champion 只接受变好的 candidate**。这个方向和之前项目分析一致：离线调用大模型进化 Skill，在线只执行冻结后的可审计代码。

## 6. 如果 GLM 自由写 Python 没效果，就换成 DSL + 参数优化

我建议马上把大模型角色降维，不要让 GLM 直接写完整 Python。更稳的方案是：

```yaml
skill_type: cycle
score_formula:
  queue: 1.0
  waiting_time: 0.25
  hunger: 0.6
  downstream_queue: -1.2
  upstream_release_pressure: 0.4
allocation:
  method: pressure_softmax
  min_green: 10
  max_green: 60
guards:
  all_phases_served: true
  max_cycle_jump: 20
  downstream_block_clip: true
```

然后：

```text
GLM 负责：
  提出 feature 组合、公式结构、保护规则、失败修复建议

CMA-ES / Bayesian Optimization / grid search 负责：
  调 w_queue、w_wait、w_downstream、w_hunger、switch_penalty 等连续参数

SUMO sealed evaluator 负责：
  判定是否真的比 incumbent 好
```

这比"GLM 自由写代码"稳定得多，也更像 Transportation Science / OR 期刊能接受的方法：**LLM-guided structured policy search + deterministic simulation-based acceptance**。

如果这一套还没有效果，再换第二层方法：**离线 oracle imitation**。也就是对每个状态，在 SUMO 里离线用短时域 rolling horizon / MPC / exhaustive limited search 找一个较优动作，然后让 GLM/DSL 进化去拟合这些 oracle 行为。这样大模型不是凭空发明，而是压缩"离线优化器"的规律。

## 7. 现在应该马上补的实验矩阵

不要只跑 `FixedTime / MaxPressure / SignalClaw / Seed / Evolved`。下一轮至少要跑：

```text
FixedTime
MaxPressure-CyclicAllocation
MaxPressure-QueueOnly
MaxPressure-Canonical
MaxPressure-SwitchLossAware
SignalClaw legacy
SignalClaw-Seed artifact
SignalClaw-Evolved-Champion 或 seed_fallback
```

MaxPressure 这块尤其重要。当前代码虽然做了 movement-level 三层策略，但如果没有显式 `phase_downstream_mapping`，仍可能回退到启发式或全局平均。 所以要报告每个 MaxPressure 变体，不能只叫一个 "MaxPressure"。

场景也不能只用一个 route。仓库里已经有 `ScenarioCatalog.default_catalog()` 的设计，包括 base、morning_peak、evening_peak、low_demand、mainroad_imbalance、leftturn_surge、mixed_stress 等场景。 下一步应该把这些场景真的生成并接入 sealed evaluation。

## 8. 我建议下一轮开发任务按这个优先级排

**P0：证明新代码真的跑了。**
重跑实验，更新 `results/summary.json`，必须带新版 metric 字段和 controller stats。旧 summary 不要再作为当前证据。

**P1：Seed artifact parity。**
先让 `SignalClaw-Seed` 接近 legacy `SignalClaw`。如果 Seed 还差，GLM 进化没有意义。

**P2：sealed tournament 真正落地。**
进化输出必须统计：

```text
candidate_count
archive_pass_count
sumo_eval_count
paired_eval_count
accepted_champion_count
seed_fallback_count
```

如果 `accepted_champion_count=0`，这不是失败，而是说明系统正确拒绝了坏候选。

**P3：DSL 化，不要自由 Python。**
GLM 输出 DSL/patch，编译器生成 Python，参数优化器调权重，evaluator 做裁判。

**P4：多场景、多 seed。**
至少 7 个场景 × 5 个 seeds。没有这个，不要说方法有效。

**P5：cohort-level search。**
单路口 champion 通过后，再做全网 cohort 组合。因为某个路口局部变好，可能把拥堵推给下游。

## 9. 对 `gpt_pro_suggestion_round1.md` 的处理

我不建议直接把这个建议文件原样提交。它可以留在工作区，等你把其中内容整理成正式的：

```text
docs/roadmap_v1_3_meta_optimization.md
```

再提交。正式文档应该写成：问题诊断、实验验收标准、方法路线、开发任务，而不是聊天建议稿。

## 最后一句话

现在项目下一步不是"继续让 GLM 多生成几个 Skill"，而是：

> **先证明实验真的跑了；再证明 Seed artifact 复现 legacy；然后把 GLM 进化改成 DSL 候选池 + 参数优化 + sealed tournament。只有这样，基于大模型的离线 Skill 优化才可能稳定地产生正效果。**

如果按这个路线跑完，Evolved-Champion 仍然没有任何 accepted improvement，那就明确换方法：从"LLM 自由代码进化"切到"离线 oracle + DSL/参数优化 + LLM failure analysis"。这不是放弃大模型，而是把大模型放到更可靠的位置。

[1]: https://deepmind.google/discover/blog/funsearch-making-new-discoveries-in-mathematical-sciences-using-large-language-models/ "FunSearch: Making new discoveries in mathematical sciences using Large Language Models — Google DeepMind"
[2]: https://deepmind.google/discover/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/ "AlphaEvolve: A Gemini-powered coding agent for designing advanced algorithms — Google DeepMind"
