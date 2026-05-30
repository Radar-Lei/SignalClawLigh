我看完现在 `main` 上的结果和代码后，判断是：**你的怀疑非常合理，但现在不能先说“Max Pressure 理论不如 Fixed Time”，也不能先说“GLM 进化没用”。更像是 baseline 实现、指标口径、闭环执行方式、进化选择压力这四件事混在一起了。**

当前 `results/summary.json` 里，MaxPressure 并不是所有指标都比 FixedTime 差：它的 `avg_queue`、`max_queue`、`total_throughput`、`avg_travel_time`、`completed_vehicles` 都比 FixedTime 好，但 `avg_waiting_time` 和 `avg_delay` 更差；SignalClaw-Evolved 则确实在 `avg_queue`、`avg_waiting_time`、`avg_delay`、`completed_vehicles` 上比 SignalClaw-Seed 更差，只是在 `avg_travel_time` 上比 Seed 好一些。也就是说，你看到的“有些指标变好，有些反而更差”在当前结果里是成立的。([GitHub][1])

---

## 1. Max Pressure 为什么可能比 Fixed Time 差？

这里我不建议先从理论上解释，而是先从实现上排查。现在仓库里的 `MaxPressureSkill` 更像是一个 **cycle-based pressure allocation baseline**，不是严格意义上的经典 Max Pressure。代码注释里也写了：它是“all phases are served in order, but green time is allocated proportionally to pressure”的周期式变体。([GitHub][2])

经典 Max Pressure 的核心通常是：按 movement 或 phase 计算上游队列减下游队列的压力，然后在决策时选择压力最大的相位；已有介绍也把它概括为：每个 movement 的 pressure 是 upstream queue 减 downstream queue，再结合饱和流，路口选择 pressure 最大的相位。([ROSA P][3]) 但当前代码有几个关键偏差。

第一，当前 `compute_pressure()` 里面的 downstream pressure 是把整个路口的所有 downstream queue 汇总后平均，再作为同一个下游惩罚项影响每个 phase。这不是 movement-specific downstream pressure。结果就是：每个相位真正区分度主要来自 incoming queue，下游拥堵影响反而变成一个全局平移项。([GitHub][2])

第二，当前做法会把所有 phase 的压力平移成正数，然后按比例分配绿灯。这样即使某个 phase 的真实 pressure 是负的，它仍然会拿到至少 `min_green`，这和“选择最大 pressure 相位”的思想不一样。拥堵时这会造成两个副作用：高压相位拿不到足够连续服务，低压相位也被强行服务，等待时间可能变差。([GitHub][2])

第三，runner 里对 MaxPressure / legacy SignalClaw 的执行方式是：**只在进入一个新的绿色相位时调用 `plan_cycle()`，然后只用 `setPhaseDuration()` 改当前绿灯持续时间**。它没有真正让 MaxPressure 在每个 decision interval 上自由选择“下一相位”。这会把 MaxPressure 退化成“固定相序 + 动态配绿”，如果 Fixed Time 本身相序/offset 已经比较适合当前 SUMO 场景，MaxPressure 反而容易破坏原本协调。([GitHub][4])

第四，当前指标口径本身需要先修。runner 每 10 步采一次指标，`throughput` 用的是采样当步的 `getArrivedIDList()`，而 `completed_vehicles` 又来自单独的车辆追踪；`stops` 还是 `int(total_queue * 0.3)` 这种 proxy。这会导致“total_throughput”和“completed_vehicles”不完全一致，当前结果里也确实出现了这种不一致。([GitHub][4])

所以 MaxPressure 的排查优先级应该是：

```text
先不要拿现在这个 MaxPressure 当强 baseline 结论。
先实现一个 canonical / movement-level MaxPressure，再比较。
```

我建议至少做 4 个 MaxPressure 版本：

```text
MP-Current:
  现在这个版本，保留作为历史对照。

MP-QueueOnly:
  只按 phase incoming queue 分配，去掉 downstream 项。
  用来判断 downstream 计算是否在制造噪声。

MP-CanonicalSelect:
  每个 decision interval 选择 pressure 最大相位；
  满足 min_green 后才允许切换；
  pressure 按 movement-specific upstream - downstream 算。

MP-CyclicPressure:
  保留固定相序，但每周期只做配绿；
  downstream 也必须 movement-specific。
```

如果 `MP-QueueOnly` 比 `MP-Current` 好，说明当前 downstream pressure 计算有问题。
如果 `MP-CanonicalSelect` 明显好，说明问题主要是“把 MaxPressure 做成固定相序周期配绿”导致的。
如果所有 MP 都不如 FixedTime，那才进一步怀疑场景 demand、phase mapping、检测器、路网 offset、黄灯损失和 SUMO 默认配时本身。

---

## 2. 为什么净化 / 进化后的 CycloClaw 会比手写 seed 差？

这个现象我反而觉得很正常。因为当前“进化”已经有目录和 artifact 体系了，但从结果看，**选择压力还没有真正落到 SUMO 闭环交通指标上**。

例如一个 evolved cycle skill 的 manifest 里，`replay_score` 是 0.9434，但 `sumo_score`、`mean_waiting`、`mean_queue`、`throughput` 都还是 0.0。也就是说，这个候选被记录为 frozen/evolved，但它并没有携带真实的 SUMO 闭环性能证据。([GitHub][5])

`evolution_summary.json` 里也能看到 cycle score 有提升，phase score 很多是 1.0；这意味着 phase evaluator 很可能已经饱和，无法区分好坏。([GitHub][6]) 一旦 evaluator 分不出 phase skill 的优劣，LLM 生成出来的“看起来合理”的 phase code 就可能被误选。

还有一个更直接的问题：evolved skill 里用了 `predicted_arrival`，例如 score 里有 `q * 1.0 + arr * 1.5 + wt * 0.2`，但 runner 构造 observation 时 `predicted_arrival=0.0`。([GitHub][7]) 这会造成一个很尴尬的局面：GLM 进化出来的公式以为自己在利用预测到达，但真实闭环里这个信号恒为 0，相当于进化目标和执行环境不一致。

再看 seed skill，它反而有一些“土但稳”的机制：队列、等待、下游 spillback proxy、hunger bonus、历史平滑等。([GitHub][8]) 这些手写启发式可能不漂亮，但对交通控制很重要。LLM 进化如果没有行为回归测试，很容易把这些隐含保护删掉或稀释掉。于是就会出现：AST 过了，replay 分数也不错，但闭环 SUMO 里更差。

还有一个工程层面的风险：`evolved_cohort.json` 里面很多路径是 `/home/samuel/projects/...` 这种绝对路径，而且 cohort 里混用了 evolved skill 和 seed v0000 skill。([GitHub][9]) 这会影响可复现性，也会让实验结果解释变复杂：你以为跑的是全 evolved cohort，实际可能是一部分 evolved、一部分 seed，甚至路径不一致时发生 fallback / load failure。

所以净化后变差的核心原因，我会概括成一句话：

> **现在的净化更像“代码可执行化 / AST 安全化”，还不是“交通行为净化 / 闭环性能净化”。**

---

## 3. AST 结构化输出要做，但 AST 不能当最终保险

仓库里已经有 `ASTSandbox`，它会检查危险 import、危险函数、接口、确定性、class、try/except、复杂度等。这个方向是对的。([GitHub][10]) 但是 AST 只能回答：

```text
这段代码能不能安全解析？
有没有明显危险调用？
接口是不是 plan(obs) / decide(obs, plan)？
```

它回答不了：

```text
相位映射是不是错了？
绿灯分配是不是交通上合理？
会不会让某个相位长期饥饿？
会不会破坏下游？
会不会导致 cycle 波动太大？
会不会只是在 replay 里好看，SUMO 闭环里变差？
```

所以结构化输出应该升级成三层。

第一层是 **JSON schema 输出**，不让 GLM 自由吐代码：

```json
{
  "skill_type": "cycle",
  "version_note": "...",
  "features_used": [
    "queue",
    "waiting_time",
    "downstream_queue",
    "hunger_time"
  ],
  "parameters": {
    "w_queue": 1.0,
    "w_wait": 0.2,
    "w_downstream": -0.8,
    "w_hunger": 0.6
  },
  "invariants": {
    "min_green": 10,
    "max_green": 60,
    "min_cycle": 40,
    "max_cycle": 180,
    "all_phases_served": true
  },
  "code": "def plan(obs): ..."
}
```

第二层是 **受限 DSL / expression tree**，最好不要一开始就让它写任意 Python。比如周期 skill 只允许表达：

```text
phase_score =
    w_q * queue
  + w_w * waiting_time
  + w_arr * predicted_arrival
  + w_h * hunger
  - w_down * downstream_spillback
  - w_switch * switch_penalty
```

LLM 可以提出 feature 组合和结构，但最终编译器自己把 DSL 编译成 Python。这样 AST 通过率会高很多，也能防止奇怪语法和动态行为。

第三层是 **行为契约测试**，这比 AST 更关键：

```text
队列增加，相位分数不能下降太多。
某相位长时间未服务，hunger bonus 必须上升。
下游严重堵塞时，对应放行相位不能继续大幅加绿。
总绿灯不能超过 cycle 上限。
所有必须相位都要被服务。
同一 obs 多次调用输出必须一致。
```

也就是说，净化不能只是：

```text
LLM rewrite -> AST pass -> 保存
```

而应该是：

```text
LLM/DSL candidate
  -> schema pass
  -> AST pass
  -> unit tests pass
  -> behavioral contract pass
  -> replay safety pass
  -> micro-SUMO pass
  -> full-SUMO sealed pass
  -> 才能进入 champion
```

---

## 4. META Optimization 应该怎么用？

我建议把“Meta Optimization”分成两类：**LLM 结构搜索** 和 **数值参数优化**。不要让 LLM 同时负责所有事情。

FunSearch 和 AlphaEvolve 的共同点不是“相信大模型写的程序”，而是：LLM 只是生成候选程序，真正的裁判是自动 evaluator 和进化式筛选。FunSearch 明确是让程序能被自动运行和评估，再保留高分程序继续进化；AlphaEvolve 也是把 LLM 的代码生成能力和自动 evaluator 结合，在进化框架里改进候选。([Google DeepMind][11])

OPRO 的思想也可以借鉴：把历史候选和分数放进 prompt，让 LLM 基于“之前哪些方案得分高/低”提出下一批候选，而不是单轮让它凭空写一个最优算法。([arXiv][12]) TextGrad 则更像把 evaluator 的文字反馈反传给候选组件，用文本反馈来优化复杂 AI 系统。([arXiv][13])

落到 SignalClaw / CycloClaw，我建议这样做：

```text
LLM 负责：
  1. 提出新的评分公式结构
  2. 增删 feature
  3. 根据失败案例修复规则
  4. 解释为什么某个指标变差
  5. 生成 DSL / patch，而不是随意生成整文件 Python

非 LLM optimizer 负责：
  1. 调 w_queue / w_wait / w_downstream / w_hunger 等连续参数
  2. 用 Bayesian Optimization / CMA-ES / grid search 做小维度搜索
  3. 用 Pareto front 选 champion
  4. 用 sealed SUMO evaluator 做最终裁判
```

最实用的进化闭环应该是：

```text
Seed Skill
  ↓
LLM 生成 20 个结构变体
  ↓
AST + DSL + unit tests 筛掉 70%
  ↓
Replay safety 筛掉 50%
  ↓
参数优化器给剩下候选调权重
  ↓
micro-SUMO 快速评估
  ↓
full-SUMO 多 seed / 多 demand sealed 评估
  ↓
Pareto selector 选 champion
  ↓
失败案例进入 archive，下一轮 prompt 使用
```

这里的关键是：**LLM 不当裁判，LLM 只当 proposal generator。**

---

## 5. 不能完全依靠大模型，这点你判断是对的

这个项目里，大模型一定要用，但不应该“完全依靠大模型”。正确分工应该是：

```text
大模型：
  负责产生候选结构、解释失败、做局部 patch、组合已有策略。

AST / DSL / schema：
  负责保证代码形态、接口、安全性、可解析性。

传统优化器：
  负责调连续参数和阈值。

SUMO evaluator：
  负责判断真实闭环交通表现。

Safety layer：
  负责保证上线执行永远不越界。

Archive / Pareto selector：
  负责避免只看单指标，避免回退。
```

这也和我们之前定的方向一致：**离线 LLM 进化 Skill，在线只执行冻结后的确定性 Skill，不在线调用大模型，不在线探索动作空间。** 

尤其交通信号控制不能只看一个 score。下一版 selector 不应该只按 replay score 排，应该强制加门槛：

```text
硬门槛：
  AST 必须过
  unit tests 必须过
  safety violations = 0
  phase starvation = 0
  sumo_score 必须是真实评估，不允许 0/空
  completed_vehicles 不得比 seed 下降超过 1% 或 2%
  avg_waiting 不得比 seed 上升超过 3% 或 5%
  avg_queue 不得比 seed 上升超过 3% 或 5%
  safety_clip_count 不得显著增加

软目标：
  avg_travel_time 越低越好
  avg_waiting 越低越好
  avg_queue 越低越好
  throughput/completed 越高越好
  downstream spillback 越低越好
  cycle volatility 越低越好
  code complexity 越低越好
```

当前 selector 里已经有多目标权重的雏形，但它的核心还是 replay score、复杂度、安全违规、starvation、以及如果有 sumo_report 才加入 SUMO 分。([GitHub][14]) 下一步应该把 `sumo_report` 变成 champion 的必需条件，而不是 optional bonus。

---

## 6. 我建议下一轮马上做的排查与改造

### A. 先把指标修干净

先不要继续争论谁优谁劣。当前 `total_throughput`、`completed_vehicles`、`stops` 口径不一致。建议统一成：

```text
completed_vehicles:
  SUMO arrived vehicle count / tripinfo count

throughput_per_hour:
  completed_vehicles / simulated_hours

avg_travel_time:
  completed vehicles 的真实 travel time 均值

avg_waiting_time:
  tripinfo waitingTime，或者明确说明是 lane-level sample waiting

avg_queue:
  每 step / 每 lane halting number 的时间平均

total_stops:
  如果 SUMO tripinfo 有 stops 就用真实 stops；
  不要用 int(queue * 0.3) 当最终结论指标。
```

否则 MaxPressure 和 Evolved 的优劣很容易被指标口径误导。

### B. 重写 MaxPressure baseline

把现在这个版本保留叫 `MaxPressure-CyclicAllocation`，再新加：

```text
MaxPressure-Canonical:
  movement-level pressure
  decision interval 级选择相位
  min_green 后允许切换
  yellow/all-red 交给 SUMO 或明确建模

MaxPressure-CyclicMovement:
  固定相序
  movement-level pressure
  只做周期配绿
```

然后用 5 个 seed 跑：

```text
FixedTime
MaxPressure-Current
MaxPressure-QueueOnly
MaxPressure-Canonical
MaxPressure-CyclicMovement
SignalClaw
```

这样才能定位 MaxPressure 差在哪里。

### C. 把 evolved champion 的门槛改掉

现在 manifest 里 `sumo_score=0.0` 的 evolved skill 不应该被标成最终 champion。它可以进入 archive，但不能进入 deployable cohort。([GitHub][5])

建议规则：

```text
archive candidate:
  AST + replay 通过即可进入 archive。

champion candidate:
  必须有真实 sumo_report。
  必须和 seed 在同一 sealed scenario 上比较。
  必须通过不退化门槛。
```

### D. 把“净化”改成 regression-preserving purification

净化不是让 GLM 把代码写得更漂亮，而是：

```text
保留 seed 的安全行为；
只修语法、接口、可读性、边界处理；
不得删除 hunger / smoothing / spillback guard；
不得改变 golden observations 上的动作超过阈值。
```

可以做一个 golden set：

```text
100 个低流量状态
100 个高流量状态
100 个下游堵塞状态
100 个单相位饥饿状态
100 个邻居释放压力状态
```

净化后 skill 在这些状态上必须满足：

```text
action 不违反约束
duration 与 seed 差异不超过 X 秒，除非 evaluator 证明更好
饥饿相位优先级不能下降
下游堵塞相位绿灯不能增加
```

### E. 让 LLM 输出 DSL，不直接输出 Python

比如周期 DSL：

```yaml
skill_type: cycle
score:
  queue: 1.0
  waiting_time: 0.25
  predicted_arrival: 0.8
  hunger: 0.6
  downstream_spillback: -1.2
cycle:
  base: 80
  queue_gain: 0.5
  min: 40
  max: 180
allocation:
  method: softmax_or_shifted_positive
  min_green: 10
  max_green: 60
guards:
  all_phases_served: true
  max_cycle_jump: 20
  downstream_block_clip: true
```

编译器自己生成 Python。这样 AST 通过率、可解释性、可调参性都会更好。

---

## 7. 最核心的判断

我会把现在的问题定性成：

```text
MaxPressure 问题：
  当前实现不是严格 MaxPressure；
  指标口径也还不够干净；
  因此它比 FixedTime 某些指标差，不一定是理论问题，更可能是 baseline 实现和评估口径问题。

Evolved / Purified Skill 问题：
  当前进化选择压力主要来自 AST / replay；
  SUMO 闭环评分没有真正成为硬门槛；
  evolved skill 使用的部分输入在 runner 里恒为 0；
  手写 seed 的隐含保护机制被进化/净化削弱；
  因此 evolved 在部分指标上比 seed 差，是预期内的。

Meta Optimization 方向：
  应该采用 FunSearch / AlphaEvolve 风格：
  LLM 生成候选，自动 evaluator 做裁判，archive 保留历史，Pareto selector 选 champion。
  不应该让 LLM 单独决定哪个 skill 好。

结构化输出方向：
  必须做。
  最好从“直接 Python”升级成“JSON schema + DSL + compiler + AST + behavior tests”。

是否完全依靠大模型：
  不应该。
  LLM 用来产生结构和修复思路；
  参数优化、约束验证、SUMO 闭环评估、上线安全层必须是确定性的。
```

下一步最值得先做的是：**修指标 + 重写 canonical MaxPressure + 把 evolved champion 的门槛改成必须有真实 SUMO sealed evaluation**。这三件事做完，MaxPressure 和净化/进化到底有没有真实问题，基本就能分清了。

[1]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/results/summary.json "raw.githubusercontent.com"
[2]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/signalclaw/skills/max_pressure.py "raw.githubusercontent.com"
[3]: https://rosap.ntl.bts.gov/view/dot/66432/dot_66432_DS1.pdf?utm_source=chatgpt.com "Learning the max pressure control for urban traffic networks"
[4]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/signalclaw/experiments/runner.py "raw.githubusercontent.com"
[5]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/artifacts/evolution_archive/evolved_skills/1159176756/cycle/v0001/manifest.json "raw.githubusercontent.com"
[6]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/artifacts/evolution_archive/evolution_summary.json "raw.githubusercontent.com"
[7]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/artifacts/evolution_archive/evolved_skills/1159176756/cycle/v0001/skill.py "raw.githubusercontent.com"
[8]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/artifacts/skills/314622964/cycle/v0000/skill.py "raw.githubusercontent.com"
[9]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/artifacts/evolution_archive/evolved_cohort.json "raw.githubusercontent.com"
[10]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/signalclaw/evolution/ast_sandbox.py "raw.githubusercontent.com"
[11]: https://deepmind.google/blog/funsearch-making-new-discoveries-in-mathematical-sciences-using-large-language-models/?utm_source=chatgpt.com "FunSearch: Making new discoveries in mathematical ..."
[12]: https://arxiv.org/abs/2309.03409?utm_source=chatgpt.com "Large Language Models as Optimizers"
[13]: https://arxiv.org/abs/2406.07496?utm_source=chatgpt.com "TextGrad: Automatic \"Differentiation\" via Text"
[14]: https://raw.githubusercontent.com/Radar-Lei/SignalClawLigh/main/signalclaw/evolution/selector.py "raw.githubusercontent.com"
