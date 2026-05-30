对，**你这个要求是必须成立的**：
进化系统不能只是“生成了一个新 Skill”，而应该是：

> **只有通过严格 sealed evaluation 并且统计上不退化的候选，才允许替换当前 champion。否则就保留原 champion。**

所以“候选可以变差”，但**champion 不能越进化越差**。现在的问题不是“GLM 没用”，而是进化管线还没有真正实现 **monotonic champion update**。当前结果里，SignalClaw-Evolved 的 `avg_queue=806.01`、`avg_waiting_time=163.61`、`completed_vehicles=7098`，确实明显差于 SignalClaw-Seed 的 `avg_queue=747.61`、`avg_waiting_time=126.89`、`completed_vehicles=7508`；这个不能被包装成成功。([GitHub][1])

## 1. 先说 MaxPressure：你说得对，它应该是强 baseline

MaxPressure 大概率应该比 FixedTime 好，至少在拥堵、需求变化、不均衡流量下，它应该在**队列、吞吐、旅行时间**这些主指标上压过普通固定配时。理论上，MaxPressure 的核心优势是基于本地上游/下游队列压力做自适应控制，并有 maximum stability 这类性质；但实际效果会被相位约束、固定周期、切换损失、turning ratio、下游容量建模影响。([科学直达][2])

你现在仓库里的情况很关键：`max_pressure.py` 已经写了四个变体，包括 `MaxPressureCyclicAllocation`、`MaxPressureQueueOnly`、`MaxPressureCanonical`、`MaxPressureCyclicMovement`，并且 `MaxPressureCanonical` 的注释说它是“每个 decision interval 选择最高 pressure phase，满足 min_green 后才切换”的 classic 版本；但文件最后仍然把 `MaxPressureSkill = MaxPressureCyclicAllocation` 作为 backward-compatible alias。([GitHub][3]) 也就是说，当前默认实验很可能跑的还是**固定相序、按压力分配绿灯的弱化版 MaxPressure**，不是你真正想拿来做强 baseline 的 canonical MaxPressure。runner 默认方法里也确实用的是 `MaxPressureSkill(decision_interval=...)`。([GitHub][4])

所以我建议马上把 baseline 分成这几条线跑清楚：

```text
FixedTime
MaxPressure-CyclicAllocation    # 当前默认，保留作历史对照
MaxPressure-QueueOnly           # 排查 downstream pressure 是否制造噪声
MaxPressure-Canonical           # 真正强 baseline
MaxPressure-CyclicMovement      # 固定相序 + movement-level pressure
SignalClaw-Seed
SignalClaw-Evolved
```

验收标准应该写死：

```text
如果 MaxPressure-Canonical 在 avg_queue / completed_vehicles / avg_travel_time 上
不能稳定好于 FixedTime，
优先怀疑：
1. phase ↔ movement mapping 错；
2. downstream queue 不是 movement-specific；
3. min_green / yellow / all-red / switching loss 没处理好；
4. 指标口径有问题；
5. FixedTime 本身是 SUMO 网络里非常强的预设配时。
```

当前结果里，MaxPressure 其实不是全面输 FixedTime：`avg_queue` 从 776.09 降到 705.32，`completed_vehicles` 从 7767 到 7859，`avg_travel_time` 从 372.82 到 359.50；但 `avg_waiting_time` 从 99.53 升到 108.87。([GitHub][1]) 这个现象更像“实现/执行方式/指标口径混合问题”，不是 MaxPressure 理论不行。

## 2. 现在 Evolved 变差的根因：不是“进化”，是“没有 champion 门槛”

现在要把“candidate”和“champion”严格分开。

```text
candidate:
  GLM 生成出来的东西。
  可以很差，可以失败，可以进入 archive 供分析。

champion:
  只有真实 SUMO sealed evaluation 通过、
  并且相对 seed / incumbent 不退化，
  才能替换线上/实验 cohort。
```

当前 `selector.py` 的文件注释已经写了“champion candidate 必须有真实 sumo_report、必须和 seed baseline 比较、必须满足硬门槛”，这方向是对的；但它的 `select()` 逻辑仍然写着如果没有 champion，就退回 archive 级候选。([GitHub][5]) 这个 fallback 对“研究 archive”可以，但对“deployable evolved cohort”不行。**没有 champion 时，应该返回 seed/incumbent，而不是返回 archive best。**

更关键的是，`run_evolution.py` 里没有找到 `SUMOEvaluator` 或 `sumo_evaluator`，实际初始化的是 `selector = SkillSelector()`，然后只创建了 `ReplayEvaluator` 并传给 `PerIntersectionEvolver`。([GitHub][6]) 这说明当前进化主路径虽然加载了 scenario catalog，但没有真正把 SUMO 闭环评估作为 candidate 替换 champion 的硬裁判。

所以现在的 Evolved 变差，不奇怪。它本质上是：

```text
GLM candidate
  -> AST / replay / prior
  -> selector fallback
  -> evolved cohort
```

而不是：

```text
GLM candidate
  -> AST / behavior tests
  -> replay safety
  -> micro-SUMO
  -> full SUMO sealed paired evaluation
  -> non-degradation gate
  -> champion update
```

## 3. M.E.T.A. Optimization 应该这样定义

我建议你把 **META Optimization** 明确定义成四层，写进项目文档和代码模块名里。

```text
M = Measurement
E = Evolution
T = Tournament
A = Archive / Acceptance
```

### M：Measurement，先把评价函数修成可信的

进化想变好，第一步不是调 prompt，而是修指标。现在 runner 每 10 步采样，`throughput` 用的是当步 `getArrivedIDList()`，`stops` 还是 `int(total_queue * 0.3)` proxy，这会导致 throughput、completed、stops 口径不一致。([GitHub][4])

必须改成：

```text
completed_vehicles:
  SUMO tripinfo / arrived vehicle count

throughput_per_hour:
  completed_vehicles / simulated_hours

avg_travel_time:
  completed vehicles 的真实 trip duration

avg_waiting_time:
  tripinfo waitingTime，或明确声明 lane-level waiting sample

avg_queue:
  每 step、每 lane halting number 的时间平均

total_stops:
  优先 SUMO tripinfo stops；
  不要用 queue * 0.3 作为最终论文指标
```

没有 Measurement integrity，META 会优化错目标。

### E：Evolution，LLM 只负责提案，不负责裁判

FunSearch 和 AlphaEvolve 的共同点不是“相信大模型”，而是**大模型生成候选程序，自动 evaluator 做裁判，进化框架保留高分候选**。FunSearch 让候选以程序形式表达，从而可以自动运行和评估；AlphaEvolve 也是把 LLM 的代码生成能力和自动 evaluator 结合，用进化框架改进候选。([Google DeepMind][7])

在 SignalClaw 里，LLM 的职责应该是：

```text
1. 生成新的评分公式结构；
2. 增删 feature；
3. 根据失败案例修 patch；
4. 总结为什么某类场景失败；
5. 输出 DSL / patch，而不是随意输出整文件 Python。
```

非 LLM optimizer 负责：

```text
1. 调 w_queue / w_wait / w_downstream / w_hunger 等连续参数；
2. 用 grid search / CMA-ES / Bayesian optimization 做小维度调参；
3. 用 Pareto selector 选候选；
4. 用 SUMO sealed evaluator 做最终裁判。
```

你之前材料里也已经把方向定得很清楚：离线进化应是“LLM 代码进化 + 自动评估器 + 多目标选择 + 参数优化”，候选要过静态检查、历史 SQL 回放、SUMO 批量仿真、多目标排序和归档。

### T：Tournament，用成对场景锦标赛替代单次分数

不能让一个候选因为单个 run 运气好就替换 seed。要做 paired tournament：

```text
同一个 traffic_input_hash
同一个 route/demand seed
同一个 detector_noise seed
同一个 incident seed
同一个 time window

Seed / Incumbent 跑一遍
Candidate 跑一遍
比较 delta
```

每个 candidate 至少跑：

```text
micro-SUMO:
  1 个 ego intersection + one-hop neighbors
  8~20 个短场景

full-SUMO:
  全网或 corridor
  5 个随机 seed
  peak / offpeak / high_demand / low_demand / incident
```

然后用硬门槛：

```text
candidate.completed_vehicles >= incumbent.completed_vehicles * 0.99
candidate.avg_waiting_time <= incumbent.avg_waiting_time * 1.03
candidate.avg_queue <= incumbent.avg_queue * 1.03
candidate.safety_violations == 0
candidate.phase_starvation == 0
candidate.safety_clip_count 不显著增加
```

再用软目标排序：

```text
J =
  1.0 * normalized_waiting
+ 1.0 * normalized_queue
+ 0.6 * normalized_travel_time
- 0.8 * normalized_completed_vehicles
+ 2.0 * safety_penalty
+ 1.5 * spillback_penalty
+ 1.0 * starvation_penalty
+ 0.3 * cycle_volatility
```

**候选必须先过硬门槛，才进入软目标排序。**

### A：Archive / Acceptance，保证 champion 单调不退化

核心规则写成一句代码逻辑：

```python
if passes_hard_gates(candidate, incumbent) and statistically_better(candidate, incumbent):
    champion = candidate
else:
    champion = incumbent
```

这就是你要的“总得越来越好”。更精确地说，是：

```text
candidate population 可以波动；
archive 可以保存坏候选；
但 deployable champion / cohort 必须单调不退化。
```

不要再允许：

```text
没有真实 sumo_report -> 也能进 evolved_cohort
sumo_score = 0/空 -> 也能当 champion
replay_score 高 -> 直接替换 seed
```

你已有材料里也提到，下一版 selector 不应该只按 replay score 排，而要强制 `sumo_score` 是真实评估、completed 不下降、waiting/queue 不恶化、安全裁剪不增加。

## 4. 现在为什么会“进化反而差”：我会重点抓 4 个 bug

第一，**SUMO 闭环没有成为硬门槛**。上面说了，`run_evolution.py` 没看到 `SUMOEvaluator` 接入，selector 虽然写了 champion 概念，但主进化流程里实际只创建了 replay evaluator。([GitHub][6])

第二，**runner 还没有真正执行 phase skill**。OnlineController 路径的注释明确说“只在检测到进入新的绿色相位时才干预，且只使用 `setPhaseDuration()` 控制绿色相位持续时间”。([GitHub][4]) 后面实际也是从 cycle plan 取 duration，然后 `traci.trafficlight.setPhaseDuration()`，并没有真正让 phase skill 做 hold/switch/extend/shorten 的闭环决策。([GitHub][4])

第三，**observation mismatch**。runner 构造 `PhaseObservation` 时 `predicted_arrival=0.0`。([GitHub][4]) 如果 evolved skill 使用 `predicted_arrival`，那它在真实运行里等于用了一个恒为 0 的假特征。你材料里也明确指出：这会让 GLM 以为自己利用了预测到达，但真实闭环里这个信号不存在，目标和执行环境错位。

第四，**净化/进化可能删掉 seed 的保护机制**。手写 seed 里通常有 hunger、smoothing、spillback guard 这类“土但稳”的保护。GLM 如果只是把代码写漂亮，或者根据 replay score 改公式，很容易削弱这些保护。你材料里建议把净化改成 regression-preserving purification：不得删除 hunger / smoothing / spillback guard，不得在 golden observations 上大幅改变动作。

## 5. 你要的“保证越来越好”，应该分两条线实现

### 线 A：单路口 skill 进化

每个路口 `i` 有当前 incumbent：

```text
CycleSkill_i_current
PhaseSkill_i_current
```

每轮进化：

```text
1. GLM 生成结构变体。
2. DSL/schema/AST 过滤。
3. 行为契约测试。
4. Replay safety。
5. 参数优化器调权重。
6. micro-SUMO_i 评估。
7. 与 current 做 paired comparison。
8. 赢了才替换 current。
```

行为契约测试必须包含：

```text
队列增加，相位优先级不能下降太多；
某相位长时间未服务，hunger bonus 必须上升；
下游严重堵塞，对应放行相位不能继续大幅加绿；
所有必须相位都要服务；
同一 observation 多次调用输出必须一致；
cycle jump 不能超过阈值。
```

你材料里已经把这层写得很对：不要让净化只是 “LLM rewrite -> AST pass -> 保存”，而应该走 schema、AST、unit tests、behavioral contract、replay safety、micro-SUMO、full-SUMO sealed 才能成为 champion。

### 线 B：cohort 级进化

交通信号不是每个路口单独最优就行。一个路口本地变好，可能把车推到下游，让 corridor 变差。所以要有 cohort selector：

```text
每个路口保留 top K 个候选：
  A: 5 个
  B: 5 个
  C: 5 个
  ...

中心做 beam search / tournament：
  组合成 cohort candidates
  在 corridor T-SUMO 里跑
  选全局不退化的 cohort
```

这和之前“中心做仲裁，不做每秒控制”的设计一致：中心负责 message broker、corridor context、skill cohort selector，而不是在线替每个路口每秒控灯。

cohort 接受规则也要写死：

```text
global_completed 不下降；
global_avg_waiting 不上升；
global_avg_queue 不上升；
任一路口 starvation 不增加；
任一路口 safety override 不增加；
下游 spillback 不增加；
通过 corridor sealed scenarios。
```

## 6. 我建议你马上改的代码规则

第一，把 selector 改成两个 API：

```python
select_archive_best(candidates) -> ArchiveEntry | None
select_deployable_champion(candidates, incumbent) -> ArchiveEntry | incumbent
```

不要再让一个 `select()` 同时负责 archive 和 champion。现在的 fallback 会把 archive best 误当 evolved best。

第二，在 manifest 里加硬字段：

```json
{
  "is_archive_candidate": true,
  "is_deployable_champion": false,
  "has_real_sumo_report": false,
  "incumbent_skill_id": "...",
  "paired_eval_passed": false,
  "accepted_for_deployment": false,
  "rejection_reason": "missing_real_sumo_report"
}
```

第三，`evolved_cohort.json` 只能引用：

```text
accepted_for_deployment = true
has_real_sumo_report = true
paired_eval_passed = true
```

否则继续用 seed/incumbent。

第四，把 `run_evolution.py` 改成真的创建并传入：

```python
sumo_evaluator = SUMOEvaluator(...)
selector = SkillSelector(sumo_evaluator=sumo_evaluator)

evolver = PerIntersectionEvolver(
    ...
    replay_evaluator=replay_evaluator,
    sumo_evaluator=sumo_evaluator,
    seed_cohort=cohort,
    scenario_catalog=scenario_catalog,
)
```

现在 `scenario_catalog_path` 虽然有参数和加载逻辑，但没有接入 `SUMOEvaluator`，这就会出现“看起来支持多场景，实际上没有用 SUMO 做 champion gate”的问题。([GitHub][6])

第五，把当前 evolved cohort 先降级命名：

```text
archive_evolved_candidates_v001.json
```

不要叫：

```text
deployable_evolved
champion
best
```

因为当前 sealed SUMO 结果已经显示它比 Seed 差。

## 7. MaxPressure 这边怎么做成“靠谱 baseline”

我建议短期改三件事。

第一，默认 baseline 不再用 alias：

```python
"MaxPressure": create_max_pressure("canonical", decision_interval=5.0)
```

不要再让 `MaxPressureSkill = MaxPressureCyclicAllocation` 偷偷作为默认强 baseline。`max_pressure.py` 现在已经有 factory，直接用 variant 名更清楚。([GitHub][3])

第二，修 movement mapping。当前 runner 能从 SUMO traffic light connections 里构造 `phase_incoming` 和 `phase_outgoing`。([GitHub][4]) 但 MaxPressure 真正需要的是：

```text
phase_id -> movements
movement -> upstream lanes
movement -> downstream lanes
movement -> saturation flow
movement -> turning ratio
```

不是简单把所有 downstream queue 平均掉。否则 pressure 就会失真。

第三，加入 switching loss aware 版本。很多 MaxPressure 论文/实践都会提醒：原始 MaxPressure 的非周期相位选择、频繁切换、忽略切换损失，会导致现实效果受损；cyclic MaxPressure 是为了工程可接受性，但可能比 original non-cyclic 弱。([科学直达][2]) 所以我们的 canonical baseline 不能粗暴每 5 秒乱切，要有：

```text
min_green
max_green
yellow/all-red loss
cooldown
pressure hysteresis
switch penalty
```

最终 baseline 表应该这么报：

```text
FixedTime
MaxPressure-CyclicAllocation
MaxPressure-Canonical
MaxPressure-SwitchLossAware
SOTL
SignalClaw-Seed
SignalClaw-Evolved-Champion
```

如果 `MaxPressure-Canonical` 还不能大体压过 FixedTime，那就先别急着吹 SignalClaw，先查 phase mapping / pressure / metrics。MaxPressure 是我们的 sanity check。

## 8. 最终路线：不要追求每个 candidate 都变好，要保证 champion 越来越好

可以把下一阶段目标改成：

```text
v1.3 META Optimization: Monotonic Champion Evolution
```

验收标准：

```text
1. 每轮可以产生坏 candidate，但坏 candidate 只能进 archive。
2. Deployable champion 必须有真实 SUMO sealed report。
3. Champion 更新必须相对 incumbent 通过 paired non-degradation gate。
4. 如果没有 candidate 通过，champion 保持不变。
5. MaxPressure-Canonical 必须作为强 baseline 参与所有 sealed comparison。
6. Evolved cohort 如果比 Seed 差，系统必须自动回退 Seed，而不是输出 Evolved。
```

一句话：

> **Meta Optimization 的核心不是“让 GLM 每次都写出更好算法”，而是“让系统只接受真的更好的算法”。**

这才是交通控制里可靠的进化。

[1]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/results/summary.json "raw.githubusercontent.com"
[2]: https://www.sciencedirect.com/science/article/abs/pii/S0968090X20307324?utm_source=chatgpt.com "Max-pressure signal control with cyclical phase structure"
[3]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/signalclaw/skills/max_pressure.py "raw.githubusercontent.com"
[4]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/experiments/runner.py "SignalClawLigh/signalclaw/experiments/runner.py at main · Radar-Lei/SignalClawLigh · GitHub"
[5]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/signalclaw/evolution/selector.py "raw.githubusercontent.com"
[6]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/evolution/run_evolution.py "SignalClawLigh/signalclaw/evolution/run_evolution.py at main · Radar-Lei/SignalClawLigh · GitHub"
[7]: https://deepmind.google/blog/funsearch-making-new-discoveries-in-mathematical-sciences-using-large-language-models/?utm_source=chatgpt.com "FunSearch: Making new discoveries in mathematical ..."
