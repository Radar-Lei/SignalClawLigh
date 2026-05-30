我看了当前 `main` 分支。整体判断是：

**仓库已经从“手写 SignalClaw 原型”升级到了“有 artifact、evolution、selector、SUMO evaluator、OnlineController 的完整框架雏形”。但它还没有真正达到“可相信的单调进化系统”。下一步最重要的不是继续让 GLM 多生成几个 Skill，而是把 3 个闭环修严：deployable champion 门槛、真实双 Skill 在线执行、sealed SUMO 评估。**

---

## 1. 目前仓库已经更新得比较多，这是好事

现在根目录已经有 `artifacts/`、`signalclaw/`、`results/`、`sumo_scenarios/` 等目录；`signalclaw` 下也已经拆出了 `core`、`evolution`、`execution`、`experiments`、`network`、`scenario`、`skills` 等模块，说明项目结构已经明显比之前完整。([GitHub][1])

尤其是 `signalclaw/evolution` 现在不再是空壳，已经有 `glm_mutator.py`、`ast_sandbox.py`、`behavior_contracts.py`、`dsl_compiler.py`、`evaluator_replay.py`、`evaluator_sumo.py`、`per_intersection.py`、`selector.py`、`run_evolution.py` 等文件。也就是说，“离线进化 Skill”的骨架已经搭出来了。([GitHub][2])

`signalclaw/execution` 里也已经有 `cycle_manager.py`、`phase_manager.py`、`online_controller.py`、`safety_layer.py`、`stats.py`；这说明你已经开始把“周期 Skill”和“相位 Skill”的在线执行层独立出来。([GitHub][3])

`signalclaw/skills` 里现在也有 `artifact.py`、`cohort.py`、`loader.py`、`max_pressure.py`、`registry.py`、`signalclaw_skill.py`，说明 frozen skill artifact 和 cohort 加载这条线已经开始落地。([GitHub][4])

所以当前不是“方向错了”。方向是对的。现在的问题是：**几个关键地方还没有真正闭环，导致 evolved 结果可能被错误地当成 champion。**

---

## 2. 当前最严重的问题：archive candidate 仍然可能被当成 evolved champion

你的 `selector.py` 已经写出了正确思想：`select_deployable_champion()` 要求候选有真实 `sumo_report`，并且通过 paired non-degradation gate；如果没有候选通过，就应该保留 incumbent。代码里也已经有硬门槛，例如必须有 SUMO 报告、总步数不能为 0、吞吐不得低于 incumbent 的 99%、等待和排队不能超过 103%、安全违规和相位饥饿必须为 0 等。([GitHub][5])

但是 `per_intersection.py` 里仍然存在一个危险 fallback：它会先选 `archive_best`，再选 `champion`，最后返回 `champion if champion is not None else archive_best`。这意味着：**如果没有真正 deployable champion，系统仍可能退回 archive 里 replay/AST 看起来最好的候选。** 这会破坏你最想要的性质：候选可以变差，但 champion 不能越进化越差。([GitHub][6])

这件事要马上改。建议把逻辑改成：

```python
selected = selector.select_deployable_champion(
    candidates,
    incumbent=current_incumbent,
)

return selected
```

不要再 fallback 到 `archive_best`。`archive_best` 只能用于研究记录、prompt 反馈、失败案例总结，不能进入 deployable cohort。

同时，`run_evolution.py` 现在会构建并保存 `evolved_cohort.json`；保存 skill manifest 时也主要写了 `replay_score`，没有把 deployable champion 所需的 paired SUMO 证据作为硬字段写进去。([GitHub][7])

建议 manifest 立刻加这些字段：

```json
{
  "is_archive_candidate": true,
  "is_deployable_champion": false,
  "has_real_sumo_report": false,
  "paired_eval_passed": false,
  "accepted_for_deployment": false,
  "incumbent_skill_id": "...",
  "rejection_reason": "missing_real_sumo_report"
}
```

然后规定：

```text
evolved_cohort.json 只能引用 accepted_for_deployment=true 的 Skill。
如果没有候选通过，就继续引用 seed/incumbent。
```

这是下一步第一优先级。

---

## 3. 当前结果已经暴露了这个问题

`results/summary.json` 里，现在 `SignalClaw` 这个 legacy 版本表现最好：`avg_queue=531.63`、`avg_waiting_time=48.96`、`completed_vehicles=8013`。但 `SignalClaw-Seed` 明显差很多，`SignalClaw-Evolved` 又比 Seed 还差：`SignalClaw-Evolved` 的 `avg_queue=806.01`、`avg_waiting_time=163.61`、`completed_vehicles=7098`。([GitHub][8])

这说明两个问题：

第一，**新的 artifact/cohort 执行路径没有复现 legacy SignalClaw 的好结果**。所以你现在不能直接说“进化失败”，因为 seed artifact 本身就已经比 legacy SignalClaw 差很多。这里优先怀疑执行路径、phase skill 是否真的被调用、seed artifact 是否等价于 legacy code、runner 控制逻辑是否一致。

第二，**当前 Evolved 不应该被称为成功 champion**。它可以被保存到 archive，作为失败候选分析；但它不应该进入 deployable cohort。

所以短期结论要很清楚：

```text
SignalClaw legacy 有积极结果；
SignalClaw-Seed artifact path 需要先对齐 legacy；
SignalClaw-Evolved 当前不能算进化成功；
下一步先修 champion gate 和 execution parity。
```

---

## 4. 第二个严重问题：OnlineController 写得对，但 runner 没真正用完整双 Skill 闭环

`OnlineController` 的设计是对的。它的文档里明确写了 no GLM、no exploration；在 cycle boundary 调 CycleSkill，在 phase decision 调 PhaseSkill。实际代码里 `_on_cycle_boundary()` 会调用 `cycle_skill.plan()`，`_on_phase_decision()` 会调用 `phase_skill.decide()`。([GitHub][9])

但是 `runner.py` 里的 `_control_with_online_controller()` 没有真正调用这个完整闭环。它的注释和实现都显示：当前主要是在进入新绿灯相位时干预，只使用 `setPhaseDuration()` 设置当前相位持续时间；它会调用 cycle skill 生成 plan，然后从 plan 里取当前 phase 的 duration，再下发给 SUMO。([GitHub][10])

也就是说，当前实验里的 `SignalClaw-Seed` / `SignalClaw-Evolved` 很可能并没有真正执行：

```text
cycle boundary -> CycleSkill
phase decision -> PhaseSkill
```

而更像是：

```text
进入绿灯相位 -> 调 CycleSkill -> setPhaseDuration
```

这会直接削弱你的“双 Skill”架构。尤其如果 evolved phase skill 生成了很多 hold/switch/extend/shorten 逻辑，但 runner 根本没调用 `phase_skill.decide()`，那相位 Skill 的进化就没有进入闭环评估。

下一步应该把 runner 改成真正调用 OnlineController，例如：

```python
for tls_id in tls_ids:
    cmd = online_controller.step(
        tls_id=tls_id,
        sim_time=sim_time,
        raw_sumo_state=...
    )

    if cmd is not None:
        traci_executor.apply_phase_command(tls_id, cmd)
```

然后 `PhaseCommand` 必须真实落到 TraCI：

```text
hold      -> 保持当前相位，并设置剩余 duration
extend    -> 延长当前 phase duration
shorten   -> 缩短当前 phase duration，但不能低于 min_green
switch    -> 切到 next_phase_id，处理 yellow/all-red
```

验收标准也要写死：

```text
cycle_plan_count > 0
phase_decision_count > 0
phase_hold_count / switch_count / extend_count / shorten_count 有真实记录
safety_clip_count 有日志
online_glm_calls = 0
```

---

## 5. 第三个严重问题：SUMO evaluator 还不是 deployable champion 的必需条件

`evaluator_sumo.py` 已经存在，而且设计目标是 T-SUMO offline evaluator：替换目标路口的候选 Skill，其他路口保持原 cohort，然后跑多 seed 的离线仿真。([GitHub][11])

但 `run_evolution.py` 里 `_try_create_sumo_evaluator()` 是 graceful fallback：如果没有 scenario catalog 或相关依赖，就跳过 SUMO evaluator，只用 ReplayEvaluator。([GitHub][7])

这个 fallback 对“开发调试”可以接受，但对“生成 deployable evolved cohort”不能接受。建议分成两个模式：

```text
archive mode:
  可以没有 SUMO evaluator。
  只保存 candidate 到 archive。
  不写 deployable evolved_cohort。

deployable mode:
  必须有 SUMO evaluator。
  必须有 scenario catalog。
  必须有 paired candidate-vs-incumbent 结果。
  没有候选通过就保持 seed/incumbent。
```

CLI 上可以加：

```bash
--archive-only
--require-sumo-for-champion
--write-deployable-cohort
```

默认建议：

```text
没有 SUMO sealed evaluation，就不能写 evolved_cohort.json。
```

---

## 6. 第四个问题：`predicted_arrival` 仍然是 0，但进化 Skill 可能会依赖它

`runner.py` 里现在 `DEFAULT_PREDICTED_ARRIVAL = 0.0`，并且注释里也写了 TODO：真实 predictor 还没有接入；`enable_prediction` 时也还是 placeholder。([GitHub][10])

这很危险。因为 GLM 生成 Skill 时可能会写：

```python
score = queue * 1.0 + predicted_arrival * 1.5 + waiting * 0.2
```

但真实 runner 里 `predicted_arrival` 永远是 0。于是进化环境和执行环境不一致，候选在 replay/prompt 里看起来合理，SUMO 闭环里却退化。

下一步要做 feature availability gate：

```json
{
  "queue": true,
  "waiting_time": true,
  "downstream_queue": true,
  "neighbor_pressure": true,
  "predicted_arrival": false
}
```

然后：

```text
如果 predicted_arrival=false：
  prompt 里禁止使用 predicted_arrival；
  DSL compiler 不允许该 feature；
  AST / behavior test 检查候选代码是否访问该字段；
  访问则 reject。
```

或者反过来，先把 prediction 接上：

```text
SUMO short-horizon arrival predictor
SQL prediction_record replay
简单 travel-time shifted upstream release predictor
```

在 prediction 没接好之前，不要让 GLM 用这个特征。

---

## 7. MaxPressure baseline 现在比之前好，但还要继续作为 sanity check

`max_pressure.py` 现在已经有五个变体：`CyclicAllocation`、`QueueOnly`、`Canonical`、`CyclicMovement`、`SwitchLossAware`。默认 alias 也已经改成 `MaxPressureCanonical`，这是正确方向。([GitHub][12])

当前 summary 里 MaxPressure 相比 FixedTime，`avg_queue`、`max_queue`、`avg_travel_time`、`completed_vehicles` 更好，但 `avg_waiting_time` 更差。([GitHub][8]) 这不是致命问题，但它应该继续作为 sanity check。

下一步建议固定跑这几个 baseline：

```text
FixedTime
MaxPressure-CyclicAllocation
MaxPressure-QueueOnly
MaxPressure-Canonical
MaxPressure-SwitchLossAware
SignalClaw legacy
SignalClaw-Seed artifact
SignalClaw-Evolved champion
```

这里的关键不是证明 MaxPressure 一定最强，而是用它检查：

```text
phase mapping 是否正确
movement downstream queue 是否正确
runner 是否真的调用 decide()
metrics 是否一致
yellow/all-red/switch loss 是否处理合理
```

如果 `MaxPressure-Canonical` 在多 seed 下明显跑不过 FixedTime，就先别急着优化 GLM，先查 SUMO phase/movement mapping 和 runner 执行逻辑。

---

## 8. 第五个问题：`SignalClaw` legacy 和 `SignalClaw-Seed` artifact 差距太大，必须先对齐

当前结果里 legacy `SignalClaw` 明显优于 `SignalClaw-Seed`。([GitHub][8]) 这很关键。

这说明你下一步不应该直接问：

```text
为什么 evolved 比 seed 差？
```

而应该先问：

```text
为什么 seed artifact path 比 legacy SignalClaw 差？
```

建议做一个专门的 parity test：

```text
同一 SUMO 场景
同一 seed
同一 tls
同一 observation
legacy SignalClaw 输出什么？
seed artifact CycleSkill 输出什么？
seed artifact PhaseSkill 输出什么？
runner 实际下发什么？
```

记录成表：

```text
t
tls_id
current_phase
legacy_plan.green_times
seed_cycle_plan.green_times
seed_phase_command
actual_traci_command
queue
waiting
```

目标是先让：

```text
SignalClaw legacy ≈ SignalClaw-Seed artifact
```

至少在动作分布和主要指标上接近。否则后续 evolved 的比较基准是不稳的。

---

## 9. Artifact manifest 还要补“可部署证据”

现在 artifact 已经有目录和保存逻辑，但 manifest 还不够用于严肃实验。`run_evolution.py` 保存 evolved skill 时会写 `skill.py` 和 `manifest.json`，并标记 `frozen=true`、`online_learning=false`、`exploration=false`，这很好；但当前保存的 metrics 主要是 `replay_score`，没有把 SUMO sealed evidence 作为硬字段。([GitHub][7])

建议 manifest 至少补：

```json
{
  "skill_id": "...",
  "skill_type": "cycle",
  "crossing_id": "...",
  "parent_skill_ids": ["..."],
  "code_hash": "...",
  "prompt_hash": "...",
  "feature_mask": {
    "predicted_arrival": false,
    "neighbor_pressure": true
  },
  "static_check_passed": true,
  "behavior_contract_passed": true,
  "replay_passed": true,
  "has_real_sumo_report": true,
  "paired_eval_passed": true,
  "accepted_for_deployment": true,
  "incumbent_skill_id": "...",
  "scenario_hashes": ["..."],
  "route_hash": "...",
  "topology_hash": "...",
  "metrics": {
    "avg_queue_delta": -0.03,
    "avg_waiting_delta": -0.04,
    "completed_vehicles_delta": 0.01,
    "safety_violations": 0,
    "phase_starvation": 0
  }
}
```

然后 cohort 也要记录：

```json
{
  "cohort_id": "...",
  "source": "sealed_sumo_champion",
  "glm_used_online": false,
  "exploration": false,
  "all_skills_accepted_for_deployment": true
}
```

---

## 10. one-hop neighbor graph 要从“可选”变成“可验证”

OnlineController 里已经会通过 `neighbor_graph.get_neighbor_tls_ids()` 构造邻居 observation。([GitHub][9]) 但 runner / evolution 里如果 topology 缺失或为空，系统仍然可能继续跑。

这对开发方便，但对论文/实验不安全。因为你的项目目标是“多路口协同”，如果实际 neighbors 是空的，Skill 就退化成本地控制。

建议加 topology validation：

```text
每个 tls 至少要声明：
  upstream_neighbors
  downstream_neighbors
  travel_time_s
  movement_mapping
  phase_to_downstream_lanes

如果 expected_neighbor_count > 0 但实际为空：
  sealed evaluation 直接 fail
```

并在 run manifest 里写：

```json
{
  "topology_loaded": true,
  "tls_neighbor_counts": {
    "tls_A": {"upstream": 1, "downstream": 1},
    "tls_B": {"upstream": 2, "downstream": 1}
  }
}
```

---

## 11. 后续开发顺序建议

我建议按下面顺序做，不要跳。

### Milestone 0：禁止“变差候选”进入 deployable cohort

这是最急的。

要改：

```text
per_intersection.py
selector.py
run_evolution.py
manifest schema
cohort builder
```

验收标准：

```text
没有真实 SUMO report -> 不可部署
没有 paired incumbent comparison -> 不可部署
没有候选通过 -> 保持 seed/incumbent
archive_best 不得进入 evolved_cohort
evolved_cohort 只能引用 accepted_for_deployment=true 的 Skill
```

### Milestone 1：让 runner 真正执行双 Skill

要改：

```text
runner.py
online_controller.py / command adapter
TraCI apply command layer
stats logging
```

验收标准：

```text
cycle_skill.plan() 被调用
phase_skill.decide() 被调用
hold/switch/extend/shorten 有真实统计
setPhase / setPhaseDuration 与 PhaseCommand 对齐
yellow/all-red 不被破坏
online_glm_calls = 0
```

### Milestone 2：对齐 legacy SignalClaw 和 seed artifact

要做：

```text
同场景同 seed 跑 legacy SignalClaw
同场景同 seed 跑 seed cohort
逐步对比 observation、plan、command、TraCI 下发结果
```

验收标准：

```text
SignalClaw-Seed artifact 不应明显差于 legacy SignalClaw
否则先修 artifact path，不进入 GLM 进化
```

### Milestone 3：feature availability / predicted_arrival gating

要做：

```text
建立 feature_mask
prompt builder 只暴露可用特征
DSL compiler 禁止不可用特征
AST 检查候选代码是否访问不可用字段
```

验收标准：

```text
predicted_arrival 未接入时，候选 Skill 不能依赖 predicted_arrival
```

### Milestone 4：sealed SUMO tournament

要做：

```text
candidate vs incumbent
同 route seed
同 demand seed
同 scenario hash
多 seed
peak/offpeak/high/low/incident 场景
```

验收标准：

```text
completed_vehicles 不下降
avg_waiting 不显著上升
avg_queue 不显著上升
safety violation = 0
phase starvation = 0
通过才替换 champion
```

### Milestone 5：重新跑 baseline matrix

要跑：

```text
FixedTime
MaxPressure-CyclicAllocation
MaxPressure-QueueOnly
MaxPressure-Canonical
MaxPressure-SwitchLossAware
SignalClaw legacy
SignalClaw-Seed
SignalClaw-Evolved-Champion
```

验收标准：

```text
所有方法使用同一 metrics 定义
所有方法使用同一 route/demand/scenario seed
报告 completed、waiting、queue、travel time、safety、starvation
```

### Milestone 6：SQL cycle_state_view 接入

这一阶段再做真实 SQL：

```text
crossing_id -> SUMO tls_id
phase_id -> SUMO phase index
traffic_flow_record -> queue/flow proxy
prediction_record -> predicted_arrival
run_timing_adjustment -> historical action
wave/wave_node -> offset/coordination context
```

目标产物：

```text
artifacts/data/cycle_state_view.parquet
artifacts/data/sql_sumo_mapping.yaml
```

这一步完成后，ReplayEvaluator 才真正有业务价值。

### Milestone 7：cohort-level 进化

单路口 champion 通过后，还要做全局 cohort 验证。因为某个路口本地变好，可能把车推给下游。

建议：

```text
每个路口保留 top-K deployable candidates
中心做 beam search / tournament
选全局 corridor 不退化 cohort
```

验收标准：

```text
global completed 不下降
global waiting 不上升
任一路口 starvation 不增加
下游 spillback 不增加
```

---

## 12. 我建议你现在立刻改的 5 个点

第一，删除 `champion else archive_best` 这个 deployable fallback。archive candidate 不能替代 incumbent。

第二，`evolved_cohort.json` 只能由 `accepted_for_deployment=true` 的 skill 构建；否则继续引用 seed cohort。

第三，runner 改成真正调用 `OnlineController.step()` 或至少显式调用 `phase_skill.decide()`，否则 PhaseSkill 进化没有意义。

第四，`predicted_arrival` 未接入前，在 prompt / DSL / AST 里禁止候选使用它。

第五，先把 `SignalClaw legacy` 和 `SignalClaw-Seed artifact` 对齐，再评价 `SignalClaw-Evolved`。

---

## 13. 总结一句话

你现在的仓库已经有了正确的工程骨架，但下一阶段的核心不是“继续生成更多 Skill”，而是：

> **把 evolution 从“能生成候选”升级成“只接受真实 sealed SUMO 中不退化的 champion”；把 runner 从“主要执行周期配绿”升级成“真实执行 CycleSkill + PhaseSkill 双闭环”；把 artifact 从“保存代码”升级成“保存可部署证据”。**

这三件事做完以后，GLM 进化才会真正变成一个可靠系统，而不是一个会偶尔把坏候选写进 evolved cohort 的实验脚本。

[1]: https://github.com/Radar-Lei/SignalClawLigh "GitHub - Radar-Lei/SignalClawLigh: SignalClawLigh project workspace · GitHub"
[2]: https://github.com/Radar-Lei/SignalClawLigh/tree/main/signalclaw/evolution "SignalClawLigh/signalclaw/evolution at main · Radar-Lei/SignalClawLigh · GitHub"
[3]: https://github.com/Radar-Lei/SignalClawLigh/tree/main/signalclaw/execution "SignalClawLigh/signalclaw/execution at main · Radar-Lei/SignalClawLigh · GitHub"
[4]: https://github.com/Radar-Lei/SignalClawLigh/tree/main/signalclaw/skills "SignalClawLigh/signalclaw/skills at main · Radar-Lei/SignalClawLigh · GitHub"
[5]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/evolution/selector.py "SignalClawLigh/signalclaw/evolution/selector.py at main · Radar-Lei/SignalClawLigh · GitHub"
[6]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/evolution/per_intersection.py "SignalClawLigh/signalclaw/evolution/per_intersection.py at main · Radar-Lei/SignalClawLigh · GitHub"
[7]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/evolution/run_evolution.py "SignalClawLigh/signalclaw/evolution/run_evolution.py at main · Radar-Lei/SignalClawLigh · GitHub"
[8]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/results/summary.json "raw.githubusercontent.com"
[9]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/execution/online_controller.py "SignalClawLigh/signalclaw/execution/online_controller.py at main · Radar-Lei/SignalClawLigh · GitHub"
[10]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/experiments/runner.py "SignalClawLigh/signalclaw/experiments/runner.py at main · Radar-Lei/SignalClawLigh · GitHub"
[11]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/evolution/evaluator_sumo.py "SignalClawLigh/signalclaw/evolution/evaluator_sumo.py at main · Radar-Lei/SignalClawLigh · GitHub"
[12]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/signalclaw/skills/max_pressure.py "raw.githubusercontent.com"
