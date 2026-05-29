我按当前 `main` 分支和你上传的材料看了一遍。先给结论：**你的判断是对的——现在仓库里已经有“双 Skill”的接口和一个手写的 SignalClaw 原型，但“GLM 离线进化 Skill 的管线”基本还没有真正实现；也没有形成“每个交叉口各自独有的周期 Skill + 下个相位 Skill”的 artifact 管理体系。**

更具体地说：现在有 `signalclaw/core`、`signalclaw/skills`、`signalclaw/evolution` 这些目录，说明项目结构已经朝正确方向搭起来了；但是 `signalclaw/evolution` 目前只看到 `__init__.py`，没有 `glm_mutator.py`、`evaluator_replay.py`、`evaluator_sumo.py`、`archive.py`、`selector.py` 这些进化管线核心模块。([GitHub][1])
`signalclaw/skills` 里目前主要是 `max_pressure.py`、`signalclaw_skill.py`、`registry.py`，也就是说现在放的是**手写 Skill / baseline / registry 雏形**，不是进化出来的每路口 Skill artifact。([GitHub][2])

---

# 1. 你问“进化的 Skills 放在哪里？”——现在答案是：还没真正放

当前仓库里应该区分三种东西：

```text
1. Skill 接口
   放在 signalclaw/core/skill_api.py、state.py、constraints.py

2. 手写种子 Skill / baseline Skill
   放在 signalclaw/skills/

3. GLM 离线进化后产出的 frozen Skill artifact
   现在还没有；后续应该放在 artifacts/skills/ 或 artifacts/evolved_skills/
```

目前 `core/state.py` 和 `core/skill_api.py` 已经定义了 `NetworkObservation`、`CyclePlan`、`PhaseCommand`，以及 `CyclePlannerSkill`、`PhaseMicroSkill` 这两个协议接口，方向是对的。([GitHub][3])
`core/constraints.py` 也已经有 `min_green`、`max_green`、`min_cycle`、`max_cycle`、`max_extend`、`max_shorten` 等约束，这说明“Skill 输出先过安全约束层”这个架构已经有基础。([GitHub][4])

但是现在还缺一个很关键的东西：

```text
artifacts/skills/
  <crossing_or_tls_id>/
    cycle/
      v0001/
        skill.py
        manifest.json
        eval_report.json
    phase/
      v0001/
        skill.py
        manifest.json
        eval_report.json
  cohorts/
    cohort_2026xxxx_xxxxxx.json
```

这才应该是“进化出来的 Skill”真正存放的位置。

我不建议把所有进化结果直接塞进 `signalclaw/skills/` 源码目录。原因是：进化结果会很多，有版本、有父代、有指标、有 prompt、有 GLM 模型信息、有适用路口、有数据 split hash。它们更像实验 artifact，而不是手写源码。`signalclaw/skills/` 应该只放接口适配器、种子 Skill、加载器、模板和稳定 baseline。

---

# 2. 当前仓库已经做对了什么

先说好的一面。你现在不是从零开始了，仓库已经有几个重要基础。

## 2.1 双 Skill 的接口雏形已经有了

当前代码里已经有 `CyclePlannerSkill` 和 `PhaseMicroSkill` 两类接口。它们分别对应：

```text
CyclePlannerSkill:
  输入 NetworkObservation
  输出 CyclePlan

PhaseMicroSkill:
  输入 NetworkObservation + CyclePlan
  输出 PhaseCommand
```

这和你要的“两套 Skill”是匹配的。周期 Skill 负责下一个周期总时长、各相位绿灯分配；相位 Skill 负责下一相位或当前相位延长/缩短。([GitHub][3])

## 2.2 已经有一个手写 SignalClawSkill 原型

`signalclaw_skill.py` 里有 `SignalClawCyclePlanner`、`SignalClawMicroAdjuster`，最后组合成 `SignalClawSkill`。其中周期规划器会根据 queue、waiting proxy、下游风险、hunger bonus 等因素给相位打分，然后分配周期和绿灯时长；微调器会根据当前相位状态决定延长、缩短或切换。([GitHub][5])

这说明你现在有一个**可作为种子 Skill 的手写策略**。后续 GLM 离线进化时，可以把它作为父代，而不是从空白代码开始。

## 2.3 GLM 调用封装已经存在

`glm_client.py` 里已经有 `GLMClient`，会从环境变量 `GLM_API_KEY` 读取 key，默认模型是 `glm-5.1`。([GitHub][6])
这部分可以直接被后续 `signalclaw/evolution/glm_mutator.py` 调用。

但现在的问题是：**仓库里有 GLM client，不等于已经有 GLM 进化管线。** 目前我没有看到真正把 `GLMClient` 接到 Skill 生成、变异、修复、评估、归档里的代码。

## 2.4 当前实验结果有积极信号

`results/summary.json` 里，当前单次实验下 SignalClaw 相比 FixedTime 和 MaxPressure，在平均排队、平均等待、平均旅行时间等指标上看起来更好。例如 SignalClaw 的 `avg_queue` 是 529.13，`avg_waiting_time` 是 40.22，`avg_travel_time` 是 337.17；FixedTime 分别是 776.09、99.53、372.82；MaxPressure 分别是 705.32、108.87、359.50。([GitHub][7])

但是这个结果现在只能说是 **smoke / prototype evidence**，不能说已经证明最终方法有效。原因我下面会详细说。

---

# 3. 当前主要问题

## 问题 1：进化管线基本没实现

这是你最关心的问题。当前 `signalclaw/evolution` 目录几乎是空的，只看到 `__init__.py`。([GitHub][8])

后续至少应该有这些模块：

```text
signalclaw/evolution/
  glm_mutator.py          # 调 GLM 生成 / 修改 Skill 代码
  prompt_builder.py       # 给每个路口构造进化 prompt
  ast_sandbox.py          # 静态检查，禁止危险代码
  evaluator_replay.py     # SQL / 历史回放评估
  evaluator_sumo.py       # T-SUMO 离线仿真评估
  selector.py             # 多目标选择 / Pareto 排序
  archive.py              # 候选 Skill 档案库
  per_intersection.py     # 单路口双 Skill 进化主流程
  cohort_validator.py     # 多路口 frozen skill cohort 联合验证
```

现在没有这些，所以目前不能说“已经实现了 Skill 进化”。

## 问题 2：现在的 Skill 不是“每个交叉口独有”

当前 `SignalClawSkill` 是一个统一类，内部用字典维护一些 `intersection_state` 和 `micro_adjusters`，但核心代码逻辑还是同一套。([GitHub][5])

这和你要的目标不同。你要的是：

```text
路口 A:
  CycleSkill_A
  PhaseSkill_A

路口 B:
  CycleSkill_B
  PhaseSkill_B

路口 C:
  CycleSkill_C
  PhaseSkill_C
```

每个路口都可以有自己的权重、规则、阈值、相位偏好、邻居协调逻辑、周期稳定性策略。外部接口相同，但内部逻辑可以不同。

当前代码更像是：

```text
所有路口共用一个 SignalClawSkill 类
每个路口有一些运行时状态
但没有独立进化出的 skill.py
```

所以后续必须实现 **per-intersection skill artifact**。

## 问题 3：当前实验主循环没有真正执行“周期 Skill + 相位 Skill”双层结构

`run_experiment.py` 里，控制逻辑在进入绿灯相位时构造 observation，然后调用的是：

```python
plan = skill.plan_cycle(net_obs)
```

后面从 `plan.phase_durations` 里取当前相位时长，再调用 `traci.trafficlight.setPhaseDuration(...)`。([GitHub][9])

这意味着当前实验主要是在用周期规划结果设置相位持续时间。虽然 `SignalClawSkill` 里有 `decide()`，但当前 runner 没有形成清晰的：

```text
周期边界：调用 CycleSkill
相位边界 / 微调时刻：调用 PhaseSkill
```

所以你看到的“下个相位 Skill”现在还没有被严肃地放进在线执行闭环。

后续主循环应该改成：

```python
if is_cycle_boundary(signal_id):
    cycle_plan = cycle_skill_i.plan(network_obs)
    cycle_plan = safety_layer.clip_cycle_plan(cycle_plan, constraints)

if is_phase_decision_time(signal_id):
    phase_cmd = phase_skill_i.decide(network_obs, cycle_plan)
    phase_cmd = safety_layer.clip_phase_command(phase_cmd, constraints)
    executor.apply_phase_command(signal_id, phase_cmd)
```

这才是你要的双 Skill 执行方式。

## 问题 4：多路口协同目前还没有真正实现

现在实验 runner 里构造 `NetworkObservation` 时，`neighbors = {}`。([GitHub][9])
SUMO adapter 里虽然有 `build_network_observation`，但注释里也能看到当前只是把“其他 TLS”当作潜在 neighbor，真实系统应该使用图结构。([GitHub][10])

也就是说，现在还不是：

```text
每个路口根据自身状态 + 一跳上游 + 一跳下游状态协同控制
```

而更接近：

```text
每个路口各自看自己的状态，本地决策
```

这和你强调的“每个路口除了本身状态，还要考虑临近路口状态”还有差距。

后续必须做 one-hop topology：

```text
upstream_neighbors
downstream_neighbors
neighbor_travel_time
movement_to_downstream_lane
phase_to_neighbor_movement
offset_relation
```

然后把这些变成 `NetworkObservation.neighbors` 的真实内容。

## 问题 5：当前 observation 还没有接入 SQL 真实预测和真实流量

当前 adapter 里 `predicted_arrival` 还是 `0.0`。([GitHub][10])
实验 runner 里构造相位 observation 时，`predicted_arrival` 也还是 `0.0`。([GitHub][9])

这说明现在的实验还没有真正把你本地 SQL 里的真实输出接入 Skill 输入。

你之前的核心路线是：真实 SQL / SUMO → 统一状态抽象 → 双 Skill → 在线确定性执行；大模型只离线进化 Skill。这个原则我继续保留。
但目前仓库的 SQL 接入还没到“可支撑进化”的程度。你之前材料里也明确提到应该先构建 `cycle_state_view`，把 `traffic_flow_record`、`prediction_record`、`analyze_result`、`run_timing_adjustment`、`wave` / `wave_node` 等表转成周期级样本。

## 问题 6：当前结果指标还不能作为最终研究结论

当前结果里 SignalClaw 的 `avg_queue`、`avg_waiting_time`、`avg_travel_time` 很好，但 `total_throughput` 是 799，低于 MaxPressure 的 828。([GitHub][7])

而且 runner 里是每 10 步采一次局部 metrics，并把 `arrived_this_step` 累加成 throughput，这和最终 completed vehicles / tripinfo 指标可能不是一个严格定义。([GitHub][9])

所以现在应该写成：

```text
SignalClaw 原型在当前单场景实验里显示出 waiting / queue / travel time 改善信号；
但还不能证明其在多场景、多种子、SQL 校准、严格 sealed evaluation 下全面优于 baseline。
```

不能写成：

```text
SignalClaw 已经全面优于 FixedTime / MaxPressure。
```

---

# 4. 后续完整目标应该重新定义成什么

我建议下一阶段目标明确命名为：

```text
v1.3 Per-Intersection GLM-Evolved Dual-Skill Control
```

完整目标是：

```text
每个交叉口 i 都拥有两套独有 frozen skills：

  CyclePlannerSkill_i
    负责下一个周期：
      - cycle_length
      - phase_green_times
      - phase_order / offset preference
      - neighbor coordination tendency

  PhaseMicroSkill_i
    负责下一个相位 / 当前相位微调：
      - hold / switch / extend / shorten
      - duration_delta
      - next_phase_id
      - reason_code

这些 Skill 由 GLM 在离线阶段进化；
在线阶段只加载 frozen skill.py 并执行；
在线不调用 GLM；
在线不探索动作空间；
在线不针对同一状态尝试多个动作。
```

这个定义很关键。你现在要避免项目滑向两种错误方向：

```text
错误方向 1：
  在线大模型 agent 控灯

错误方向 2：
  传统 RL 在线探索动作空间

正确方向：
  离线 GLM 生成 / 修复 / 变异 Skill 代码
  离线 SQL replay + T-SUMO 评估
  冻结 per-intersection dual-skill cohort
  在线确定性执行
```

---

# 5. 进化后的 Skill 应该怎么存

我建议明确建立这个 artifact 规范。

```text
artifacts/
  skills/
    tls_314622964/
      cycle/
        v0001/
          skill.py
          manifest.json
          replay_report.json
          sumo_report.json
        v0002/
          skill.py
          manifest.json
          replay_report.json
          sumo_report.json
      phase/
        v0001/
          skill.py
          manifest.json
          replay_report.json
          sumo_report.json

    tls_cluster_xxx/
      cycle/
        v0001/
          skill.py
          manifest.json
      phase/
        v0001/
          skill.py
          manifest.json

    cohorts/
      cohort_20260529_001.json
      cohort_20260529_002.json
```

每个 `manifest.json` 至少包含：

```json
{
  "skill_id": "tls_314622964_cycle_v0007",
  "crossing_id": "314622964",
  "skill_type": "cycle",
  "version": 7,
  "parent_skill_ids": ["tls_314622964_cycle_v0003"],
  "code_hash": "...",
  "prompt_hash": "...",
  "data_split_hash": "...",
  "sumo_scenario_hash": "...",
  "glm_model": "glm-5.1",
  "created_at": "2026-05-29T...",
  "frozen": true,
  "online_learning": false,
  "exploration": false,
  "constraints_profile": "chengdu_default_v1",
  "metrics": {
    "replay_score": 0.0,
    "sumo_score": 0.0,
    "safety_violations": 0,
    "phase_starvation_count": 0,
    "mean_waiting": 0.0,
    "mean_queue": 0.0,
    "throughput": 0.0
  }
}
```

`cohort_*.json` 则记录某一次部署/评估使用的全网 Skill 组合：

```json
{
  "cohort_id": "cohort_20260529_001",
  "skills": {
    "314622964": {
      "cycle": "artifacts/skills/tls_314622964/cycle/v0007/skill.py",
      "phase": "artifacts/skills/tls_314622964/phase/v0005/skill.py"
    },
    "cluster_a": {
      "cycle": "artifacts/skills/tls_cluster_a/cycle/v0004/skill.py",
      "phase": "artifacts/skills/tls_cluster_a/phase/v0006/skill.py"
    }
  },
  "frozen": true,
  "glm_used_online": false,
  "created_by": "offline_evolution"
}
```

这样你以后查实验结果时可以清楚知道：

```text
哪个路口用了哪个周期 Skill？
哪个路口用了哪个相位 Skill？
这个 Skill 是哪一轮 GLM 生成的？
父代是谁？
评估结果是什么？
有没有通过安全检查？
是否 frozen？
```

---

# 6. 后续应该实现的模块

## 6.1 Skill artifact loader

新增：

```text
signalclaw/skills/artifact.py
signalclaw/skills/loader.py
signalclaw/skills/cohort.py
```

职责：

```text
读取 artifacts/skills/.../manifest.json
校验 code_hash
校验 frozen=true
校验 online_learning=false
校验 exploration=false
动态加载 skill.py
确认实现 CyclePlannerSkill 或 PhaseMicroSkill 接口
```

现在 `registry.py` 只是一个内存里的简单 registry。([GitHub][11])
后续要升级成 artifact registry，不能只在 Python 进程里注册类。

## 6.2 SQL ingestion 和 cycle_state_view

新增：

```text
signalclaw/data/sql_loader.py
signalclaw/data/cycle_state_builder.py
signalclaw/data/sql_sumo_mapping.py
```

目标不是每次训练都扫原始 SQL，而是先生成：

```text
artifacts/data/cycle_state_view.parquet
```

每一行代表一个路口、一个周期、一个相位附近的状态：

```text
crossing_id
tls_id
cycle_id
phase_id
time_of_day
day_type

phase_start_time
phase_end_time
actual_duration
base_duration
ai_duration
micro_duration

area1_car
area2_car
queue_proxy
waiting_proxy
predicted_arrival
ai_change

upstream_neighbor_pressure
downstream_spillback_proxy
offset_error
last_cycle_green_time
last_cycle_queue
next_1_cycle_outcome
next_2_cycle_outcome
```

注意：SQL replay 不能假装知道反事实收益。也就是说，历史上执行的是动作 A，你的候选 Skill 输出动作 A′，你不能直接说 A′ 会产生某个真实结果。SQL replay 主要用于：

```text
安全性检查
约束检查
稳定性检查
和历史专家 / AI 调整方向的一致性检查
高峰低峰行为合理性检查
```

真正比较候选 Skill 的交通效果，还是要在离线 T-SUMO 里做。

## 6.3 one-hop neighbor graph

新增：

```text
signalclaw/network/neighbor_graph.py
signalclaw/network/movement_mapping.py
```

当前 `neighbors={}` 的做法要替换掉。([GitHub][9])

每个路口应该有：

```json
{
  "tls_id": "314622964",
  "upstream": [
    {
      "neighbor_tls_id": "xxx",
      "from_edge": "...",
      "to_edge": "...",
      "travel_time_s": 35,
      "movement_mapping": {
        "neighbor_phase_2": "ego_phase_1"
      }
    }
  ],
  "downstream": [
    {
      "neighbor_tls_id": "yyy",
      "from_edge": "...",
      "to_edge": "...",
      "travel_time_s": 28,
      "ego_phase_to_downstream_lanes": {
        "phase_1": ["lane_a", "lane_b"]
      }
    }
  ]
}
```

然后 `NetworkObservation` 里应该包含：

```text
ego observation
one-hop upstream messages
one-hop downstream messages
neighbor message age / latency
neighbor current phase
neighbor planned next phase
neighbor queue summary
neighbor spillback summary
offset error
```

每条 neighbor message 都应该带时间戳：

```text
observed_time_s
sent_time_s
delivered_time_s
age_s
latency_s
```

这样才能避免“用未来邻居状态”的问题。

## 6.4 GLM mutator

新增：

```text
signalclaw/evolution/glm_mutator.py
```

它唯一负责调用 GLM：

```python
class GLMSkillMutator:
    def mutate_cycle_skill(
        self,
        crossing_profile,
        parent_skill_code,
        failure_cases,
        constraints,
        archive_summary,
    ) -> CandidateSkill:
        ...

    def mutate_phase_skill(
        self,
        crossing_profile,
        parent_skill_code,
        paired_cycle_skill_code,
        failure_cases,
        constraints,
        archive_summary,
    ) -> CandidateSkill:
        ...
```

它内部调用当前已有的 `GLMClient`。`GLMClient` 已经支持从 `GLM_API_KEY` 读取 key 和调用 `glm-5.1`，所以不用重写底层 API。([GitHub][6])

关键约束是：**只有 evolution 模块可以 import `glm_client.py`。**

在线执行模块必须通过测试保证：

```text
signalclaw/runner
signalclaw/adapters
signalclaw/online_controller
signalclaw/skills/loader

不能 import glm_client
不能读取 GLM_API_KEY
不能发网络请求
```

可以加一个测试：

```python
def test_online_code_does_not_import_glm():
    forbidden = ["glm_client", "requests", "zhipuai"]
    ...
```

## 6.5 AST sandbox

新增：

```text
signalclaw/evolution/ast_sandbox.py
```

GLM 生成的是代码，所以必须检查。

禁止：

```text
import
open
exec
eval
compile
__import__
globals
locals
os
sys
subprocess
socket
requests
random
time
file write
network access
```

允许：

```text
math
min
max
sum
abs
sorted
len
range
enumerate
float
int
dict/list comprehensions
```

还要检查：

```text
函数名必须正确
输入输出类型必须正确
同样输入必须确定性输出
不能修改全局状态
不能读取外部文件
不能访问未来字段
复杂度不能过高
```

## 6.6 Replay evaluator

新增：

```text
signalclaw/evolution/evaluator_replay.py
```

用于历史 SQL / cycle_state_view 的离线安全评估。

它不负责证明交通收益，只负责筛掉不合理候选：

```text
min_green_violation
max_green_violation
cycle_length_violation
phase_starvation
cycle_volatility
green_split_jump
deviation_from_historical_safe_range
neighbor_damage_proxy
downstream_blocking_risk
offset_error
```

输出：

```json
{
  "candidate_id": "...",
  "crossing_id": "...",
  "skill_type": "cycle",
  "passed": true,
  "violations": [],
  "score": 0.73,
  "failure_cases": [...]
}
```

## 6.7 SUMO evaluator

新增：

```text
signalclaw/evolution/evaluator_sumo.py
```

这才是候选 Skill 性能评估核心。

对每个路口 `i` 构建：

```text
T-SUMO_i = ego intersection i + one-hop neighbors + boundary demand profile
```

然后测试候选：

```text
固定其他路口为当前 cohort skill
只替换路口 i 的 cycle 或 phase candidate
运行多个离线 T-SUMO scenario
收集 waiting / queue / travel time / throughput / spillback / safety override
```

注意，这仍然是离线评估，不是在线探索动作空间。

## 6.8 Selector 和 archive

新增：

```text
signalclaw/evolution/selector.py
signalclaw/evolution/archive.py
```

不要只用单指标选最优。建议用多目标：

```text
waiting 越低越好
queue 越低越好
travel_time 越低越好
throughput 不得明显下降
safety_violation 必须为 0 或极低
phase_starvation 必须为 0
cycle_volatility 不能过大
neighbor_damage 不能过大
代码复杂度不能过高
```

可以先用加权分数：

```text
objective =
  + 1.00 * normalized_mean_waiting
  + 1.00 * normalized_mean_queue
  + 0.60 * normalized_travel_time
  - 0.60 * normalized_throughput
  + 2.00 * safety_violation_rate
  + 1.50 * spillback_rate
  + 1.00 * phase_starvation_rate
  + 0.50 * cycle_volatility
  + 0.50 * neighbor_damage
  + 0.05 * code_complexity
```

后面再升级成 Pareto front。

Archive 里每个候选都要保存：

```text
candidate_id
crossing_id
skill_type
parent_ids
code
code_hash
prompt
prompt_hash
glm_model
static_check_report
replay_report
sumo_report
selected / rejected reason
failure cases
```

你之前提到的“离线进化记忆”就应该落在这里，而不是做在线大模型 memory。此前方案里也明确建议保留候选 Skill 档案库，记录 skill_id、代码、prompt、父代、评估结果、失败案例、是否通过安全检查等。

---

# 7. 每个交叉口如何进化出独有双 Skill

我建议不要一开始就让 GLM 同时改所有路口、所有 Skill。那会很乱。应该按路口分治，再做 cohort 级验证。

对每个交叉口 `i`：

```text
输入：
  crossing_profile_i
  phase_constraints_i
  one_hop_neighbor_profile_i
  historical_cycle_state_i
  current_best_cycle_skill_i
  current_best_phase_skill_i
  failure_cases_i

输出：
  CyclePlannerSkill_i_vK
  PhaseMicroSkill_i_vM
```

推荐流程：

```text
Step 1：初始化种子
  CycleSkill_i_seed = 从 SignalClawCyclePlanner 拷贝出的路口 i 版本
  PhaseSkill_i_seed = 从 SignalClawMicroAdjuster 拷贝出的路口 i 版本

Step 2：单独进化 CycleSkill_i
  固定 PhaseSkill_i_seed
  GLM 生成多个 CycleSkill_i candidate
  静态检查
  SQL replay
  T-SUMO_i 评估
  选出 CycleSkill_i_best

Step 3：单独进化 PhaseSkill_i
  固定 CycleSkill_i_best
  GLM 生成多个 PhaseSkill_i candidate
  静态检查
  SQL replay
  T-SUMO_i 评估
  选出 PhaseSkill_i_best

Step 4：双 Skill 联合修复
  把 CycleSkill_i_best + PhaseSkill_i_best 放一起跑
  找失败案例
  让 GLM 做小范围 patch
  重新评估

Step 5：冻结
  写入 artifacts/skills/<tls_id>/cycle/vK/
  写入 artifacts/skills/<tls_id>/phase/vM/
```

为什么要这样做？因为周期 Skill 和相位 Skill 是耦合的。如果先把两者同时交给 GLM 任意改，失败时很难知道是周期规划错了，还是微调错了。分阶段进化更可控。

---

# 8. GLM prompt 应该怎么组织

每次让 GLM 进化 Skill，不应该问：

```text
请帮我写一个交通信号控制算法
```

这样太泛，会生成不可控代码。

应该给它非常窄的任务：

```text
你只能修改 plan() 函数体。
你不能 import。
你不能读取文件。
你不能调用网络。
你不能使用随机数。
你只能使用这些字段。
你必须返回 CyclePlan。
你必须满足 min/max green 和 min/max cycle。
下面是父代代码。
下面是失败案例。
下面是当前路口的相位、邻居、历史模式。
请生成一个更稳定的版本。
```

周期 Skill prompt 包含：

```text
crossing_id
phase_ids
phase_order
min_green / max_green
min_cycle / max_cycle
历史高峰/平峰流量
各相位平均需求
上游释放压力
下游阻塞风险
offset / green wave 需求
父代代码
父代失败案例
```

相位 Skill prompt 包含：

```text
current_phase
elapsed_green
planned_green
min_green / max_green
phase queue
predicted arrival
neighbor pressure
downstream blocked by phase
recent extension history
paired cycle skill summary
父代代码
失败案例
```

GLM 输出必须是机器可解析的：

````text
```python
def plan(obs: NetworkObservation) -> CyclePlan:
    ...
````

````

或者：

```json
{
  "rationale": "...",
  "code": "def plan(...): ...",
  "expected_effect": "...",
  "risk": "..."
}
````

但最终只把 `code` 进入 sandbox。`rationale` 只进 archive，不参与在线执行。

---

# 9. 在线执行器应该怎么改

当前 runner 的核心问题是只在绿灯入口附近调用 `plan_cycle`，并用 `setPhaseDuration` 设置持续时间。([GitHub][9])

后续在线执行器应该明确区分两个时钟：

```text
cycle boundary clock
phase decision clock
```

伪代码：

```python
for t in simulation_steps:
    observations = observation_builder.observe_all()

    for signal_id in signal_ids:
        obs_i = observations[signal_id]
        skills_i = cohort.skills[signal_id]
        constraints_i = constraints[signal_id]

        if cycle_manager.is_cycle_boundary(signal_id, t):
            raw_plan = skills_i.cycle.plan(obs_i)
            safe_plan = safety_layer.clip_cycle_plan(raw_plan, constraints_i)
            cycle_manager.set_plan(signal_id, safe_plan)

        if phase_manager.is_phase_decision_time(signal_id, t):
            active_plan = cycle_manager.get_plan(signal_id)
            raw_cmd = skills_i.phase.decide(obs_i, active_plan)
            safe_cmd = safety_layer.clip_phase_command(raw_cmd, constraints_i)
            executor.apply(signal_id, safe_cmd)

        audit_log.record(...)
```

在线执行器必须保证：

```text
不 import glm_client
不调用 GLM
不生成候选动作
不对同一状态试多个动作
不在线更新 skill.py
不在线改 manifest
```

这就是你强调的“现实落地不能探索动作空间”。

---

# 10. 多路口协同应该怎么进入 Skill

每个路口独有 Skill 不是说每个路口孤立。正确设计是：

```text
每个路口有自己的 Skill；
每个 Skill 的输入包含一跳邻居状态；
每个 Skill 的 objective 里有 neighbor damage penalty；
最终整个 cohort 一起验证。
```

对路口 `i`，状态应该是：

```python
NetworkObservation(
    ego=IntersectionObservation(...),
    neighbors={
        upstream_id: NeighborObservation(...),
        downstream_id: NeighborObservation(...),
    }
)
```

周期 Skill 应该考虑：

```text
本路口各相位需求
上游即将释放来的车辆
下游是否接得住
绿波 offset 是否需要保持
上一周期是否过度偏向某个相位
```

相位 Skill 应该考虑：

```text
当前相位是否已经满足最小绿
当前相位继续放行是否会喂入堵死的下游
下一个相位是否对应上游释放来的主流
某相位是否长期饥饿
```

一个合理的协同目标不是：

```text
只最小化本路口 queue
```

而是：

```text
减少本路口等待和排队
同时不把拥堵推给下游
同时不长期饿死上游释放来的方向
同时尽量维持走廊 offset
```

可以写成：

```text
J_i =
  local_waiting
  + local_queue
  + travel_time_proxy
  + downstream_spillback_caused
  + upstream_starvation_caused
  + offset_error
  + safety_override
  - throughput_reward
```

这就是“每个路口自治，但不伤害邻居”。

---

# 11. 当前实验结果应该怎么修

你现在应该先做一个短 milestone，不要马上堆复杂 GLM。建议叫：

```text
v1.2.2 Experiment Integrity + Skill Artifact Foundation
```

先修这些：

## 11.1 修 metrics 定义

当前 summary 里 SignalClaw 的 `completed_vehicles` 比 MaxPressure 高，但 `total_throughput` 比 MaxPressure 低，这可能是指标定义不统一导致的。([GitHub][7])

建议统一成：

```text
completed_vehicles = tripinfo arrived count
throughput_per_hour = completed_vehicles / simulated_hours
mean_travel_time = tripinfo mean duration
mean_waiting_time = tripinfo waitingTime or consistent lane-based aggregate
avg_queue = per-step lane halting number average
```

不要把“每 10 步采样的 arrived_this_step 累加”作为最终 throughput。

## 11.2 runner 必须同时执行 cycle skill 和 phase skill

现在只严肃执行了周期规划的一部分。([GitHub][9])
要改成：

```text
cycle boundary -> CyclePlannerSkill_i
phase boundary / decision interval -> PhaseMicroSkill_i
```

然后实验报告里分别记录：

```text
cycle_plan_count
phase_command_count
phase_extend_count
phase_shorten_count
phase_switch_count
safety_clip_count
safety_reject_count
```

## 11.3 把当前 SignalClawSkill 拆成 seed artifacts

当前 `SignalClawSkill` 可以保留，但要把它拆成每路口 seed：

```text
artifacts/skills/tls_xxx/cycle/v0000/skill.py
artifacts/skills/tls_xxx/phase/v0000/skill.py
```

这些不是 GLM 进化结果，而是 seed。后续 GLM 从 v0000 开始进化。

## 11.4 加 run manifest

每次实验生成：

```json
{
  "run_id": "...",
  "commit": "...",
  "sumocfg_hash": "...",
  "route_hash": "...",
  "cohort_id": "...",
  "policy": "SignalClaw-Evolved",
  "duration": 3600,
  "seed": 42,
  "online_glm_calls": 0,
  "exploration": false,
  "metrics_definition_version": "v1"
}
```

否则后续结果很难复现。

---

# 12. 建议的开发顺序

## Milestone A：把现在的“原型 Skill”变成“可冻结 Skill artifact”

交付物：

```text
signalclaw/skills/artifact.py
signalclaw/skills/loader.py
signalclaw/skills/cohort.py

artifacts/skills/<tls_id>/cycle/v0000/skill.py
artifacts/skills/<tls_id>/phase/v0000/skill.py
artifacts/skills/cohorts/seed_cohort.json
```

验收标准：

```text
每个 SUMO TLS 都有一个 cycle seed skill 和 phase seed skill
runner 不再直接实例化 SignalClawSkill 类
runner 从 cohort.json 加载 frozen skills
online_glm_calls = 0
```

## Milestone B：实现真实双 Skill 在线执行闭环

交付物：

```text
signalclaw/execution/cycle_manager.py
signalclaw/execution/phase_manager.py
signalclaw/execution/online_controller.py
```

验收标准：

```text
周期边界调用 CycleSkill
相位决策点调用 PhaseSkill
两者都经过 SafetyLayer
audit log 能区分 cycle decision 和 phase decision
```

## Milestone C：实现 one-hop neighbor observation

交付物：

```text
artifacts/topology/one_hop_neighbors.json
signalclaw/network/neighbor_graph.py
signalclaw/observation/neighbor_cache.py
```

验收标准：

```text
NetworkObservation.neighbors 不再是空字典
不再把所有 TLS 都当 neighbor
每个 neighbor message 有 timestamp / age / latency
只允许一跳邻居字段进入 Skill
```

## Milestone D：实现 SQL cycle_state_view

交付物：

```text
signalclaw/data/sql_loader.py
signalclaw/data/cycle_state_builder.py
artifacts/data/cycle_state_view.parquet
artifacts/data/sql_sumo_mapping.yaml
```

验收标准：

```text
每个 crossing_id 能映射到 SUMO tls_id
每个 phase_id 能映射到 SUMO phase index
prediction_record / analyze_result / run_timing_adjustment 能进入周期样本
Skill 输入里的 predicted_arrival 不再永远是 0.0
```

## Milestone E：实现 GLM 离线进化最小闭环

交付物：

```text
signalclaw/evolution/glm_mutator.py
signalclaw/evolution/prompt_builder.py
signalclaw/evolution/ast_sandbox.py
signalclaw/evolution/evaluator_replay.py
signalclaw/evolution/archive.py
```

先不跑 SUMO，只做：

```text
GLM 生成 candidate skill.py
AST 检查
单元测试
SQL replay 安全评估
写入 archive
```

验收标准：

```text
artifacts/skills/<tls_id>/cycle/v0001/skill.py 是 GLM 生成的
artifacts/skills/<tls_id>/phase/v0001/skill.py 是 GLM 生成的
manifest 记录 glm_model / prompt_hash / parent_skill_id
online runner 不调用 GLM
```

## Milestone F：加入 T-SUMO 离线评估

交付物：

```text
signalclaw/evolution/evaluator_sumo.py
signalclaw/evolution/per_intersection.py
```

验收标准：

```text
每个候选 Skill 都能在 T-SUMO_i 离线评估
评估结果写入 sumo_report.json
selector 能选择 champion
```

## Milestone G：cohort-level sealed validation

交付物：

```text
artifacts/skills/cohorts/cohort_evolved_v1.json
results/evolved_vs_baselines/
```

验收标准：

```text
FixedTime
MaxPressure
handwritten SignalClaw seed
GLM-evolved per-intersection dual skill

在同一套 sealed traffic inputs 上比较
报告 waiting / queue / travel time / throughput / safety / spillback / starvation
```

---

# 13. 最终完整系统长什么样

最终你要的系统应该是：

```text
                    ┌──────────────────────────┐
                    │      本地真实 SQL         │
                    │ flow / prediction / run   │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │  cycle_state_view.parquet │
                    │  sql_sumo_mapping.yaml    │
                    └────────────┬─────────────┘
                                 │
                                 ▼
        ┌────────────────────────────────────────────┐
        │              离线进化中心                   │
        │                                            │
        │  for each intersection i:                  │
        │    GLM mutate CycleSkill_i                 │
        │    GLM mutate PhaseSkill_i                 │
        │    AST sandbox                             │
        │    SQL replay evaluator                    │
        │    T-SUMO_i evaluator                      │
        │    selector / archive                      │
        └────────────────────┬───────────────────────┘
                             │
                             ▼
        ┌────────────────────────────────────────────┐
        │        Frozen Skill Artifact Registry       │
        │                                            │
        │  tls_A/cycle/v0007/skill.py                │
        │  tls_A/phase/v0005/skill.py                │
        │  tls_B/cycle/v0004/skill.py                │
        │  tls_B/phase/v0008/skill.py                │
        │  cohort_evolved_v1.json                    │
        └────────────────────┬───────────────────────┘
                             │
                             ▼
        ┌────────────────────────────────────────────┐
        │              在线执行 / R-SUMO              │
        │                                            │
        │  observe ego + one-hop neighbors           │
        │  cycle boundary -> CycleSkill_i            │
        │  phase decision -> PhaseSkill_i            │
        │  safety layer                              │
        │  TraCI setPhase / setPhaseDuration         │
        │  audit log / metrics                       │
        │                                            │
        │  No GLM                                    │
        │  No exploration                            │
        │  No online learning                        │
        └────────────────────────────────────────────┘
```

---

# 14. 我对当前仓库状态的最终判断

现在仓库状态可以这样概括：

```text
已经有：
  - 双 Skill 接口雏形
  - 手写 SignalClaw seed skill
  - GLM client
  - SUMO 实验 runner
  - FixedTime / MaxPressure / SignalClaw 初步对比
  - 安全约束基础

还没有：
  - GLM 离线进化管线
  - 每个交叉口独有的 cycle skill / phase skill artifact
  - skill archive / manifest / cohort registry
  - SQL cycle_state_view 接入
  - 真实 one-hop neighbor observation
  - 清晰的周期 Skill + 相位 Skill 在线执行分层
  - 严格 sealed、多场景、多种子的最终研究证据
```

所以接下来最应该做的不是继续调一个更复杂的手写 SignalClaw 公式，而是先把项目从：

```text
手写双 Skill 原型
```

升级成：

```text
每路口 frozen dual-skill artifact + GLM 离线进化闭环 + cohort 级验证
```

最短开发路径就是：

```text
1. 先建 artifacts/skills/ 规范
2. 把当前 SignalClaw 拆成每路口 seed cycle/phase skills
3. 改 runner，让它真正加载 cohort 并分别调用 cycle skill 和 phase skill
4. 建 SQL cycle_state_view
5. 建 one-hop neighbor graph
6. 实现 GLM mutator + AST sandbox + replay evaluator
7. 加 T-SUMO evaluator
8. 每个路口分别进化 cycle skill 和 phase skill
9. 冻结 cohort，回到 R-SUMO 做 sealed comparison
```

这样才会真正满足你的核心要求：**大模型只在离线阶段调用 GLM；每个交叉口进化出自己独有的周期 Skill 和下个相位 Skill；在线控灯时只执行冻结后的可审计代码，不调用大模型，也不探索动作空间。**

[1]: https://github.com/Radar-Lei/SignalClawLigh/tree/main/signalclaw "SignalClawLigh/signalclaw at main · Radar-Lei/SignalClawLigh · GitHub"
[2]: https://github.com/Radar-Lei/SignalClawLigh/tree/main/signalclaw/skills "SignalClawLigh/signalclaw/skills at main · Radar-Lei/SignalClawLigh · GitHub"
[3]: https://github.com/Radar-Lei/SignalClawLigh/raw/refs/heads/main/signalclaw/core/skill_api.py "raw.githubusercontent.com"
[4]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/core/constraints.py "SignalClawLigh/signalclaw/core/constraints.py at main · Radar-Lei/SignalClawLigh · GitHub"
[5]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/skills/signalclaw_skill.py "SignalClawLigh/signalclaw/skills/signalclaw_skill.py at main · Radar-Lei/SignalClawLigh · GitHub"
[6]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/glm_client.py "SignalClawLigh/glm_client.py at main · Radar-Lei/SignalClawLigh · GitHub"
[7]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/results/summary.json "SignalClawLigh/results/summary.json at main · Radar-Lei/SignalClawLigh · GitHub"
[8]: https://github.com/Radar-Lei/SignalClawLigh/tree/main/signalclaw/evolution "SignalClawLigh/signalclaw/evolution at main · Radar-Lei/SignalClawLigh · GitHub"
[9]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/experiments/runner.py "SignalClawLigh/signalclaw/experiments/runner.py at main · Radar-Lei/SignalClawLigh · GitHub"
[10]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/adapters/sumo_traci.py "SignalClawLigh/signalclaw/adapters/sumo_traci.py at main · Radar-Lei/SignalClawLigh · GitHub"
[11]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/signalclaw/skills/registry.py "SignalClawLigh/signalclaw/skills/registry.py at main · Radar-Lei/SignalClawLigh · GitHub"
