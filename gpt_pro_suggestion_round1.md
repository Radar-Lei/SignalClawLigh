我看完后的核心判断是：**这个项目不要做成“在线大模型控灯”，而应该做成“离线大模型进化 Skill + 在线确定性安全执行器”**。在线运行时只执行已经冻结、可审计、可复现的代码 Skill；GLM/LLM 只在离线优化 Skill 时调用。

你的公开仓库目前主要包含 `docs`、`sumo_scenarios/chengdu`、`glm_client.py`、`requirements.txt` 等内容；`glm_client.py` 已经有通过环境变量 `GLM_API_KEY` 调用 GLM 的封装，默认模型是 `glm-5.1`，但还没有完整的交通对，这一点必须修正。**SQL 数据不应该作为和当前 SUMO 场景一一匹配的 replay 数据集，也不需要做 `SQL crossing_id ↔ SUMO tls_id`、`SQL phase_id ↔ SUMO phase index` 这种强对齐。**

更准确的定位应该是：

> **SQL 是真实交通系统的数据参考，用来提取真实业务分布、控制约束、相位调整习惯、流量波动模式、预测误差模式和多路口协调特征；SUMO 是我们自己的实验环境，用 SQL 的统计规律去构造、校准和扩展仿真场景。**

所以之前我说“SQL cycle_state_view 接入 SUMO”“SQL-SUMO mapping”这部分要改掉。不是完全不要 SQL，而是 SQL 的角色从 **“仿真真值/回放数据”** 改成 **“真实世界先验/场景生成参考/GLM 进化提示材料”**。

---

# 1. 修正后的核心架构

应该改成这样：

```text
真实 SQL 数据
  ↓
SQL Reference Profiler
提取真实交通规律、相位规律、预测规律、调整规律
  ↓
SUMO Scenario Generator
根据真实规律构造多组仿真流量、扰动、邻接压力、绿波场景
  ↓
Per-Intersection GLM Skill Evolution
每个 SUMO 路口独立进化 cycle skill + phase skill
  ↓
SUMO Offline Evaluation
只在 SUMO 中评估候选 Skill
  ↓
Frozen Skill Cohort
在线仿真 / 控灯时只执行冻结 Skill，不调用 GLM
```

也就是说，SQL 和 SUMO 的关系不是：

```text
SQL 路口 A = SUMO 路口 A
SQL 相位 1 = SUMO 相位 1
```

而是：

```text
SQL 提供真实交通系统的统计规律
SUMO 使用这些规律生成更像真实场景的仿真任务
```

这会让系统更合理，也更容易实现。

---

# 2. 哪些内容要从之前方案里删掉或弱化

之前方案里这些内容要改：

```text
不再要求：
  SQL crossing_id ↔ SUMO tls_id 强映射
  SQL phase_id ↔ SUMO phase index 强映射
  SQL direction_id ↔ SUMO lane / edge 强映射
  SQL actual_duration ↔ SUMO phase duration 强映射
  SQL 历史回放直接评估 Skill 效果
```

尤其是这个要删掉：

```text
candidate Skill 在 SQL 历史状态 S_t 上输出动作 A'_t，
然后用 SQL 后续结果 O_t 评价 A'_t 好不好
```

这个逻辑本来就危险，因为 SQL 里记录的是历史实际执行动作的后果，不是候选动作的反事实后果。现在你明确说 SQL 和 SUMO 场景不匹配，那就更不能做这种 replay evaluation。

应该改成：

```text
SQL 只做 reference profiling，不做候选 Skill 的直接效果评估。
候选 Skill 的效果评估全部在 SUMO 离线仿真中做。
```

---

# 3. SQL 现在应该怎么用

SQL 应该转成一个“真实交通参考画像”，我建议叫：

```text
artifacts/reference/sql_reference_profile.json
```

或者：

```text
artifacts/reference/traffic_domain_priors.json
```

它不是训练集，也不是回放数据，而是一个统计先验文件。

## 3.1 从 SQL 提取周期规律

例如：

```json
{
  "cycle_duration_prior": {
    "min": 60,
    "p25": 80,
    "median": 100,
    "p75": 120,
    "max": 160,
    "peak_median": 120,
    "offpeak_median": 80
  }
}
```

用途：

```text
给 SUMO 里的 cycle skill 设置合理搜索范围；
给 GLM prompt 提供真实系统里常见周期长度；
给安全约束层设置 min_cycle / max_cycle 的参考。
```

不是说 SUMO 必须用 SQL 里的某个具体周期，而是说 SUMO 里的周期设计不要脱离真实交通控制经验。

---

## 3.2 从 SQL 提取相位绿灯规律

例如：

```json
{
  "phase_green_prior": {
    "major_through": {
      "median": 35,
      "p75": 45,
      "max_safe": 60
    },
    "minor_left": {
      "median": 15,
      "p75": 22,
      "max_safe": 35
    },
    "pedestrian_or_minor": {
      "median": 12,
      "p75": 18
    }
  }
}
```

用途：

```text
构造 Skill 的初始种子；
限制 GLM 进化不要生成离谱绿灯；
构造 SUMO 场景中不同相位的默认配时。
```

由于 SQL 和 SUMO 相位不一一对应，这里可以按“相位类型”来归纳，而不是按具体 `phase_id`。

比如 SUMO 里的某个相位被标成：

```text
主路直行
主路左转
支路直行
支路左转
混合相位
```

然后从 SQL 的统计画像里取对应类型的先验。

---

## 3.3 从 SQL 提取流量时间分布

例如：

```json
{
  "demand_time_profile": {
    "morning_peak": {
      "start": "07:30",
      "end": "09:00",
      "relative_intensity": 1.45
    },
    "midday": {
      "relative_intensity": 0.75
    },
    "evening_peak": {
      "start": "17:00",
      "end": "19:00",
      "relative_intensity": 1.60
    },
    "night": {
      "relative_intensity": 0.35
    }
  }
}
```

用途：

```text
生成 SUMO route / flow 文件；
生成不同 demand scenario；
测试 Skill 是否只适合单一流量，而不是高峰、平峰、低峰都能稳。
```

也就是说，SQL 不用和当前 `chengdu.rou.xml` 对齐，但可以告诉我们：

```text
高峰大概比平峰高多少？
早高峰和晚高峰哪个更强？
流量波动是平滑的还是脉冲式的？
```

---

## 3.4 从 SQL 提取方向不均衡模式

真实路口常见问题不是总流量大，而是某些方向突然很偏。

可以从 SQL 里提取：

```json
{
  "directional_imbalance_patterns": [
    {
      "name": "main_road_dominant",
      "major_minor_ratio": 3.5
    },
    {
      "name": "left_turn_surge",
      "left_turn_multiplier": 2.2
    },
    {
      "name": "upstream_platoon_arrival",
      "pulse_interval_s": 90,
      "pulse_width_s": 25
    }
  ]
}
```

用途是构造 SUMO 场景：

```text
主路强流
支路强流
左转突增
上游车团到达
下游拥堵反压
潮汐流
```

这些比“让 SUMO 场景完全复制 SQL 路口”更有价值。

---

## 3.5 从 SQL 提取微调规律

你之前说 SQL 里有实际输出，它非常适合用来分析：

```text
什么时候会微调？
微调幅度通常是多少？
是 +3 秒、+5 秒，还是 +10 秒？
哪些情况下缩短？
哪些情况下延长？
微调是否常发生在高峰？
微调是否有上限？
```

可以生成：

```json
{
  "micro_adjustment_prior": {
    "common_extend_values": [3, 5, 8],
    "common_shorten_values": [-3, -5],
    "max_extend_recommended": 8,
    "max_shorten_recommended": 5,
    "high_confidence_extend_conditions": [
      "current_phase_queue_high",
      "downstream_not_blocked",
      "next_phase_pressure_low"
    ]
  }
}
```

用途：

```text
约束 PhaseMicroSkill；
给 GLM 生成微调 Skill 的 prompt；
避免 GLM 写出每次都延长 20 秒这种不现实策略。
```

---

## 3.6 从 SQL 提取预测误差模式

如果 SQL 里有预测记录和实际流量记录，那么可以估计：

```text
预测提前量
预测误差分布
高峰预测是否更不准
低峰预测是否更稳定
方向流量预测是否容易偏
```

然后在 SUMO 里构造 prediction noise：

```json
{
  "prediction_error_profile": {
    "horizon_s": [30, 60, 120],
    "mean_absolute_percentage_error": {
      "30": 0.12,
      "60": 0.18,
      "120": 0.27
    },
    "peak_error_multiplier": 1.3
  }
}
```

用途：

```text
Skill 输入里可以有 predicted_arrival；
但这个 predicted_arrival 不是 SQL 原始预测值；
而是 SUMO 当前检测数据 + SQL 误差规律生成的仿真预测值。
```

这样就很合理。

---

# 4. SUMO 场景应该怎么根据 SQL 参考来构造

当前 SUMO 场景不需要和 SQL 完全对应，但要从一个简单场景扩展成多组实验场景。

我建议建立：

```text
artifacts/scenarios/
  base/
  peak_morning/
  peak_evening/
  low_demand/
  directional_imbalance/
  left_turn_surge/
  upstream_platoon/
  downstream_spillback/
  prediction_noise/
  mixed_stress/
```

每个 scenario 都可以由同一个 `chengdu.net.xml` 派生不同的 `.rou.xml` 或 demand profile。

目录类似：

```text
sumo_scenarios/
  chengdu/
    chengdu.net.xml
    base.rou.xml
    morning_peak.rou.xml
    evening_peak.rou.xml
    imbalance_main_road.rou.xml
    left_turn_surge.rou.xml
    downstream_bottleneck.rou.xml
    prediction_noise_high.rou.xml
```

然后加一个生成器：

```text
signalclaw/scenario/
  sql_reference_profiler.py
  demand_generator.py
  route_mutator.py
  scenario_catalog.py
```

核心逻辑是：

```python
sql_profile = SQLReferenceProfiler(...).build_profile()

ScenarioGenerator(
    net_file="chengdu.net.xml",
    base_route_file="chengdu.rou.xml",
    reference_profile=sql_profile,
).generate_all()
```

它输出：

```text
一组更接近真实业务分布的 SUMO 测试场景
```

而不是输出 SQL-SUMO 映射。

---

# 5. GLM 离线进化时怎么使用 SQL

GLM 进化 Skill 时，prompt 里应该包含两类材料：

```text
1. SUMO 路口自身材料
   tls_id
   phase structure
   lanes controlled by each phase
   one-hop neighbors
   current scenario metrics
   previous candidate failure cases

2. SQL 参考画像
   realistic cycle range
   realistic green range
   common micro adjustment range
   demand imbalance patterns
   prediction noise profile
   coordination / offset prior
```

也就是说，GLM 不是根据 SQL 某个路口生成某个 SUMO 路口的 Skill，而是根据：

```text
SUMO 拓扑 + SUMO 仿真失败案例 + SQL 真实业务先验
```

去进化每个 SUMO 路口自己的 Skill。

这更符合你的意思。

---

# 6. 每个 SUMO 交叉口仍然要有独有 Skill

这个要求不变，而且应该更明确：

```text
每个 SUMO tls_id 都有：
  CycleSkill_<tls_id>
  PhaseSkill_<tls_id>
```

即使 SQL 和 SUMO 不匹配，也不影响这个设计。因为“独有 Skill”的依据是 SUMO 场景里的路口拓扑和仿真表现，而不是 SQL 里的真实路口 ID。

例如：

```text
SUMO tls_A:
  主路流量大、下游容易堵
  → 进化出更保守的下游放行策略

SUMO tls_B:
  上游车团明显、需要协调 offset
  → 进化出更强的上游 platoon 接收策略

SUMO tls_C:
  支路容易饿死
  → 进化出更强的 phase starvation 保护
```

SQL 只告诉它：

```text
真实世界中周期一般不要太短/太长；
微调幅度通常不应过大；
高峰流量波动是什么样；
预测误差大概有多强；
绿波 offset / 多路口协调一般有什么结构。
```

---

# 7. 之前的 Milestone D 应该改成什么

之前我说：

```text
Milestone D：实现 SQL cycle_state_view
```

现在应该改成：

```text
Milestone D：实现 SQL Reference Profiler + SUMO Scenario Generator
```

新的交付物：

```text
signalclaw/reference/sql_profiler.py
signalclaw/reference/profile_schema.py
signalclaw/scenario/demand_generator.py
signalclaw/scenario/scenario_catalog.py

artifacts/reference/sql_reference_profile.json
artifacts/scenarios/scenario_catalog.json
sumo_scenarios/chengdu/generated/*.rou.xml
```

验收标准：

```text
不要求 SQL 路口 ID 和 SUMO tls ID 对齐
不要求 SQL phase ID 和 SUMO phase index 对齐
能从 SQL 提取真实周期、绿灯、微调、流量、预测误差分布
能基于这些分布生成多组 SUMO demand scenario
GLM prompt 能读取 sql_reference_profile.json
SUMO evaluator 能在 generated scenarios 上评估候选 Skill
```

这比原来的强映射方案更稳。

---

# 8. 当前进化管线也要随之调整

修正后的进化管线应该是：

```text
for each SUMO intersection tls_i:

  1. 读取 SUMO tls_i 拓扑
     - phase count
     - controlled lanes
     - incoming/outgoing edges
     - one-hop neighbors

  2. 读取 SQL reference profile
     - cycle prior
     - green split prior
     - micro adjustment prior
     - demand pattern prior
     - prediction error prior

  3. 读取当前 seed skill
     - CycleSkill_i_v0000
     - PhaseSkill_i_v0000

  4. 在 SUMO scenario set 上评估 seed
     - base
     - peak
     - imbalance
     - platoon
     - spillback
     - noise

  5. 找 failure cases
     - 某场景排队过高
     - 某相位饥饿
     - 吞吐下降
     - 下游溢出
     - 周期剧烈波动
     - 安全层频繁裁剪

  6. 调用 GLM 离线生成候选 Skill
     - 只生成代码
     - 不在线调用
     - 不在线探索

  7. AST / sandbox 检查

  8. SUMO 多场景评估

  9. selector 选择 champion

  10. 写入 artifact
      artifacts/skills/<tls_i>/cycle/vXXXX/
      artifacts/skills/<tls_i>/phase/vXXXX/
```

注意这里已经没有：

```text
SQL replay evaluator
```

最多只有：

```text
SQL prior consistency checker
```

它只检查候选 Skill 是否明显背离真实先验，例如：

```text
周期经常给 300 秒
某相位经常 2 秒绿
微调动辄 +40 秒
完全忽视预测误差
```

但它不负责评价交通收益。

---

# 9. 新的模块划分

我建议现在项目后续按这个目录走：

```text
signalclaw/
  core/
    state.py
    skill_api.py
    constraints.py
    safety_layer.py
    metrics.py

  reference/
    sql_profiler.py
    profile_schema.py
    prior_checker.py

  scenario/
    demand_generator.py
    route_mutator.py
    scenario_catalog.py
    prediction_noise.py

  network/
    sumo_topology.py
    neighbor_graph.py
    movement_mapping.py

  skills/
    artifact.py
    loader.py
    cohort.py
    seeds/
      cycle_seed.py
      phase_seed.py

  evolution/
    glm_mutator.py
    prompt_builder.py
    ast_sandbox.py
    evaluator_sumo.py
    selector.py
    archive.py
    per_intersection.py
    cohort_validator.py

  execution/
    cycle_manager.py
    phase_manager.py
    online_controller.py

  experiments/
    run_baselines.py
    run_evolved_cohort.py
    compare_scenarios.py
```

其中 `reference/` 是专门处理 SQL 参考，不再叫 `data/sql_loader.py` 或 `sql_sumo_mapping.py`，避免误解。

---

# 10. SQL Reference Profile 的建议 schema

可以先定义成这样：

```json
{
  "metadata": {
    "source": "local_sql",
    "used_as": "domain_reference_only",
    "requires_sumo_alignment": false,
    "created_at": "..."
  },

  "cycle_duration_prior": {
    "min_recommended": 60,
    "max_recommended": 160,
    "median": 100,
    "peak_median": 120,
    "offpeak_median": 80
  },

  "phase_green_prior": {
    "min_green_recommended": 8,
    "max_green_recommended": 60,
    "typical_major_phase": [30, 45],
    "typical_minor_phase": [12, 25]
  },

  "micro_adjustment_prior": {
    "max_extend_recommended": 8,
    "max_shorten_recommended": 5,
    "common_extend_seconds": [3, 5, 8],
    "common_shorten_seconds": [-3, -5]
  },

  "demand_patterns": {
    "morning_peak_multiplier": 1.45,
    "evening_peak_multiplier": 1.60,
    "low_demand_multiplier": 0.35,
    "directional_imbalance_ratios": [2.0, 3.0, 4.0]
  },

  "prediction_error_prior": {
    "horizon_seconds": [30, 60, 120],
    "mape": {
      "30": 0.12,
      "60": 0.18,
      "120": 0.27
    }
  },

  "coordination_prior": {
    "use_one_hop_neighbors": true,
    "offset_sensitive": true,
    "downstream_spillback_sensitive": true
  }
}
```

这个文件可以进 prompt，也可以进 scenario generator，也可以进 safety layer。

---

# 11. SUMO 场景生成器的建议输入输出

输入：

```text
chengdu.net.xml
chengdu.rou.xml
sql_reference_profile.json
scenario_config.yaml
```

输出：

```text
generated_base.rou.xml
generated_morning_peak.rou.xml
generated_evening_peak.rou.xml
generated_mainroad_imbalance.rou.xml
generated_leftturn_surge.rou.xml
generated_upstream_platoon.rou.xml
generated_downstream_spillback.rou.xml
generated_prediction_noise_high.rou.xml
```

`scenario_catalog.json`：

```json
{
  "scenarios": [
    {
      "name": "base",
      "route_file": "generated_base.rou.xml",
      "weight": 1.0,
      "description": "baseline demand"
    },
    {
      "name": "evening_peak",
      "route_file": "generated_evening_peak.rou.xml",
      "weight": 1.5,
      "description": "SQL-inspired evening peak demand"
    },
    {
      "name": "leftturn_surge",
      "route_file": "generated_leftturn_surge.rou.xml",
      "weight": 1.2,
      "description": "SQL-inspired directional imbalance"
    }
  ]
}
```

进化时不要只跑一个场景，而是跑 scenario set：

```text
candidate_score =
  weighted_score(base)
  + weighted_score(morning_peak)
  + weighted_score(evening_peak)
  + weighted_score(imbalance)
  + weighted_score(spillback)
  + weighted_score(prediction_noise)
```

这样进化出的 Skill 才不会过拟合某个 `.rou.xml`。

---

# 12. 对“不能探索动作空间”的进一步修正

你说的“不能探索动作空间”主要约束的是在线真实执行。现在 SQL 不做回放真值之后，逻辑更清楚了：

```text
线上：
  绝不探索
  同一状态只执行 frozen skill 的唯一输出
  不调用 GLM
  不生成多个动作比较

离线：
  可以在 SUMO 里评估多个候选 Skill
  因为这是仿真，不是真实道路
  但候选生成、筛选、比较都发生在离线实验环境
```

所以我们不是用 SQL 来做反事实探索，而是用 SUMO 来做离线评估。SQL 只负责让 SUMO 场景更贴近真实业务分布。

---

# 13. 最终项目定义也要改一句

之前可以说：

> 系统使用真实 SQL 数据进行历史回放和状态建模，使用 SUMO/TraCI 做数字孪生仿真评估。

现在应该改成：

> **系统使用真实 SQL 数据提取交通需求、配时、微调、预测误差和协调模式等领域先验，并据此构造更贴近真实业务分布的 SUMO 仿真场景；GLM 只在离线阶段基于 SUMO 拓扑、仿真失败案例和 SQL 领域先验进化每个路口独有的周期 Skill 与相位 Skill；在线阶段只执行冻结后的 Skill，不调用大模型，不进行动作探索。**

这才是准确版本。

---

# 14. 修正后的优先级

现在后续优先级应该改成：

```text
第一优先级：
  skill artifact / cohort 机制
  每个 SUMO tls_id 有独有 cycle skill 和 phase skill

第二优先级：
  runner 真正执行 cycle skill + phase skill 双层闭环

第三优先级：
  SUMO one-hop neighbor graph
  让每个路口的 observation 包含临近路口状态

第四优先级：
  SQL Reference Profiler
  从 SQL 提取真实业务先验，不做强匹配

第五优先级：
  SUMO Scenario Generator
  根据 SQL profile 生成多组 demand / stress scenarios

第六优先级：
  GLM offline evolution pipeline
  调 GLM 生成候选 skill.py，AST 检查，SUMO 多场景评估，archive 保存

第七优先级：
  每路口独立进化 + cohort-level 联合验证
```

所以我会把之前的路线修正成：

```text
不是：
  SQL → 对齐 SUMO → replay → 进化 Skill

而是：
  SQL → 提取真实交通先验 → 生成/校准 SUMO 场景 → SUMO 离线评估 → GLM 进化每路口 Skill
```

这个修正非常重要，也更符合你的项目实际情况。
信号控制执行框架。SUMO 场景在 `sumo_scenarios/chengdu` 下，包含 `chengdu.net.xml`、`chengdu.rou.xml` 和 `chengdu.sumocfg`，配置时间大约是 0 到 3599.75 秒，也就是当前更像一个 1 小时仿真场景。([GitHub][1])

你仓库里的 SQL 说明文档非常关键。它描述的是一个真实交通信号智能控制系统导出的 MySQL 数据，时间跨度大约是 2025-11 到 2026-05，包含 3 个真实路口、约 6 个月数据，且覆盖采集、AI 预测、配时优化、指令下发和 SUMO 仿真相关字段。虽然真实 `.sql` 文件没上传，但文档里已经能看出输入输出边界：`traffic_flow_record` 是主流量表，约 3670 万条；`prediction_record` 有预测结果；`analyze_result` 里有类似 `{base, change}` 的 AI 调整 JSON；`run_timing_adjustment` 里有 `base_duration`、`ai_duration`、`micro_duration`、`actual_duration`；还有 `wave`、`wave_node` 这类绿波/多路口协调表。([GitHub][2])

---

## 1. 推荐的总体架构

我建议拆成四层：

```text
真实 SQL / SUMO
      ↓
统一状态抽象 TrafficState / NetworkState
      ↓
两级 Skill：CyclePlannerSkill + PhaseMicroSkill
      ↓
在线执行器：SUMO TraCI 或真实信控接口
```

这里最重要的是：**Skill 是普通 Python 函数或小型 DSL 程序，不是 prompt，不是每次调用大模型的 agent。**

### 在线部分

在线运行时只做这几件事：

```text
读取当前路口状态
读取邻近路口状态
调用已冻结的 CyclePlannerSkill 或 PhaseMicroSkill
通过安全约束裁剪输出
下发信号控制命令
记录输入、输出、约束裁剪、执行结果
```

SUMO 侧可以通过 TraCI 控制仿真中的信号灯。SUMO 官方文档里，TraCI 是外部脚本和运行中的仿真之间的 TCP 客户端/服务器接口，脚本可以获取仿真状态并发送控制指令；交通灯控制 API 包括 `setPhase`、`setPhaseDuration`、`setProgram`、`setProgramLogic` 等，其中 `setPhaseDuration` 可以设置当前相位的剩余时长。([日食SUMO][3])

### 离线部分

离线才调用 GLM/LLM：

```text
历史 SQL + SUMO 场景 + 已有 Skill
      ↓
生成候选 Skill 代码
      ↓
静态检查 / 单元测试 / 约束检查
      ↓
历史回放评估
      ↓
SUMO 批量仿真评估
      ↓
多目标排序
      ↓
冻结新版本 Skill
```

这样既满足你说的“大模型用来进化 Skill”，也满足“实时执行不调用大模型”。

---

## 2. 两套 Skill 应该怎么定义

你说的两套 Skill 可以非常清晰地定义成上下两层。

### Skill A：周期规划 Skill

这个 Skill 是“总规”。它在每个周期开始前，或者在需要重规划时运行一次。

输入：

```python
NetworkState:
    current_intersection_state
    neighbor_intersection_states
    current_cycle_id
    current_time
    predicted_arrivals
    queue_proxy_by_phase
    last_cycle_plan
    wave_coordination_state
    historical_context_window
```

输出：

```python
CyclePlan:
    cycle_length
    phase_green_times: dict[phase_id, seconds]
    phase_order
    offset_targets
    coordination_notes
```

它要回答的问题是：

```text
下一个周期总时长是多少？
每个相位分多少绿灯？
是否需要照顾邻近路口的释放流？
是否需要保持绿波偏移？
是否需要压制下游溢出？
```

你 SQL 里的 `crossing_phase` 已经有 `min_green_time`、`max_green_time`、`lane_count`、`micro_weight` 这类约束字段；`crossing` 里有 `fixed_cycle_duration`、`micro_adjust_mode`、`signal_dispatch_mode`；`run_timing_adjustment` 里又有实际执行时长链路，所以周期规划 Skill 的输出可以自然落到这些字段附近。([GitHub][2])

### Skill B：相位微调 Skill

这个 Skill 是“执行层微调”。它不重新发明整个周期，只在周期规划 Skill 给出的框架内做小范围调整。

输入：

```python
PhaseState:
    current_phase_id
    elapsed_green
    planned_green
    min_green
    max_green
    queue_proxy
    predicted_arrival_short_horizon
    neighbor_pressure
    downstream_spillback_risk
```

输出：

```python
PhaseCommand:
    action: hold / switch / extend / shorten
    duration_delta
    next_phase_id
    reason_code
```

微调 Skill 只能做受限动作，例如：

```text
当前相位最多延长 +5 秒
当前相位最多缩短 -5 秒
不得低于 min_green_time
不得超过 max_green_time
不得跳过强制相位
不得破坏黄灯 / 全红安全间隔
不得让任何相位长期饥饿
```

也就是说，**周期 Skill 管大方向，微调 Skill 管最后几秒钟的局部响应**。

---

## 3. 多路口协同怎么做

多路口协同不应该做成“所有路口一个巨大黑箱”。我建议建一个图：

```text
IntersectionGraph:
    node = crossing
    edge = upstream/downstream/neighbor relation
```

每个路口的 Skill 输入包含三类状态：

```text
1. 自身状态
   当前相位、各相位排队、预测到达、上一周期执行结果

2. 上游状态
   上游哪个相位正在放行、预计多少车会在 20/30/60 秒后到达本路口

3. 下游状态
   下游是否拥堵、是否有溢出风险、当前能不能继续向下游释放车辆
```

一个实用的协同评分可以长这样：

```python
score = (
    local_pressure
    + alpha * upstream_release_pressure
    - beta * downstream_spillback_risk
    - gamma * offset_error
    - lambda_ * switching_penalty
)
```

其中：

```text
local_pressure：本路口压力
upstream_release_pressure：上游即将释放到本路口的车流压力
downstream_spillback_risk：下游阻塞风险
offset_error：和绿波目标 offset 的偏差
switching_penalty：频繁切换惩罚
```

你的 SQL 文档里已经有 `wave`、`wave_beat`、`wave_node`，并且包含 `crossing_id`、`phase_id`、`offset` 等字段，这正好可以作为多路口绿波/偏移协调的初始数据结构。([GitHub][2])

---

## 4. PI-Light 应该借鉴什么，不应该照搬什么

PI-Light 最值得借鉴的是这个思想：

> 搜索一个可解释、可执行的小程序，然后把这个程序注入到交通信号 agent 里执行。

PI-Light 的仓库里，`02_run_MCTS.py` 会创建环境和每个路口的 `PiLight` agent，然后用 `MCTS_synthesizer.begin_search(64)` 搜索程序，再通过 `distribute(...)` 把搜索到的代码分发给 agent 执行。评估指标包括 travel time、queue length、delay、throughput。([GitHub][4])

PI-Light 的 agent 里有 `inject_code`，执行时通过代码片段计算每个 movement/phase 的价值，再选分数最高的相位；它的 DSL 里有类似 `inlane_2_num_vehicle`、`inlane_2_num_waiting_vehicle`、`outlane_2_num_vehicle`、`vehicle_dist` 这类特征。([GitHub][5])

但是你的项目不应该直接照搬 PI-Light 的两个点：

第一，PI-Light 里有 raw `exec` 执行代码片段的机制；真实信控系统里建议改成**受限 AST / DSL / 纯函数插件**，避免任意代码执行。([GitHub][5])

第二，PI-Light 更偏“相位选择”；你的目标还要控制“周期长度”和“每个相位持续时间”。这里 PI-eLight 更接近你的需求，因为它明确说竞赛环境里每次决策需要同时指定 phase 和 duration，并且在 π-Light 基础上增加了决定是否保持当前相位的程序。([GitHub][6])

所以我的建议是：

```text
参考 PI-Light 的“程序化策略 + 搜索 + 注入执行”
参考 PI-eLight 的“相位 + 持续时间”思想
但不要照搬它的 exec 和在线探索方式
```

---

## 5. 最关键约束：不能探索动作空间

你这句话非常重要：

> 同一交通状态下，不能尝试不同 Skill 方案然后评价哪个好；现实落地不能探索动作空间。

我的理解是：**不能在真实系统/在线系统里探索动作空间**。因此不能做传统在线 RL，也不能让大模型在线生成多个动作试错。

可行的替代方案是三层评估：

### 第一层：历史回放评估

用 SQL 里的真实历史数据重放状态序列：

```text
历史状态 S_t
历史实际方案 A_t
历史后续结果 O_t
```

候选 Skill 在 `S_t` 上输出 `A'_t`。这里不能声称知道 `A'_t` 的真实后果，因为历史里没有执行过它。但可以评估：

```text
是否违反 min/max green
是否过度偏离已有安全配时
是否造成相位饥饿
是否导致周期剧烈波动
是否符合历史专家/AI 调整方向
是否在高峰/低峰下行为合理
```

这叫**离线一致性和安全性评估**，不是反事实真实收益评估。

### 第二层：校准 SUMO 数字孪生评估

把 SQL 里的真实流量、预测、周期、相位映射到 SUMO 场景，校准需求输入，然后在 SUMO 中批量评估候选 Skill。

这一步可以比较不同 Skill，因为它发生在离线仿真里，不是在真实道路上探索。

### 第三层：保守上线

真实系统上线时只允许：

```text
影子模式 shadow mode：只输出建议，不控灯
小范围 A/B 不是随机探索，而是预设时段/路口灰度
超出安全阈值立即回退到固定配时或已有 AI 配时
```

这样可以避免“在线探索动作空间”的问题。

---

## 6. 离线进化 Skill 的技术路线

我建议采用：

```text
LLM 代码进化 + 自动评估器 + 多目标选择 + 参数优化
```

这和 FunSearch / AlphaEvolve 的思想接近：LLM 负责生成或修改程序，程序必须能被自动运行和评估，最终通过进化式循环保留更好的候选。FunSearch 使用大模型生成程序并由 evaluator 自动评估；AlphaEvolve 也强调由 LLM 生成代码变体，再通过自动评估器筛选改进。([Google DeepMind][7])

一个完整离线循环可以这样：

```text
1. 初始化 Skill 种子
   固定配时
   车辆数压力控制
   max-pressure
   绿波 offset 规则
   历史 AI change 模仿规则

2. LLM 生成候选 Skill
   只允许改指定函数体
   只允许使用白名单特征
   不能 import
   不能读写文件
   不能访问网络
   不能随机

3. 静态检查
   AST 检查
   类型检查
   复杂度检查
   单元测试
   安全约束检查

4. 历史 SQL 回放
   看稳定性、合规性、和历史策略的一致性

5. SUMO 批量仿真
   看 travel time、queue、delay、throughput、stop count、spillback

6. 多目标排序
   不是只看一个指标，而是 Pareto 选择

7. 归档
   保存 skill 代码、版本、指标、失败案例、适用场景
```

OPRO、TextGrad、DSPy 这类方法也可以作为辅助：OPRO 可把历史候选及分数放进 prompt，让 LLM 产生更好的候选；TextGrad 可以把 evaluator 的文字反馈反传给候选代码；DSPy 更适合把“生成候选、修复候选、总结失败原因”这些 LLM 调用流程模块化。([arXiv][8])

我的建议优先级是：

```text
第一优先级：FunSearch / AlphaEvolve 风格的代码进化
第二优先级：数值参数用 Bayesian Optimization / CMA-ES
第三优先级：OPRO/TextGrad/DSPy 做 prompt 和反馈组织
不要做：在线 RL 探索
```

---

## 7. Skill 的代码接口建议

可以先定义一套强约束接口：

```python
from dataclasses import dataclass
from typing import Dict, List, Protocol, Optional


@dataclass(frozen=True)
class PhaseObservation:
    phase_id: int
    queue: float
    waiting_time: float
    predicted_arrival: float
    elapsed_green: float
    last_green: float
    min_green: float
    max_green: float


@dataclass(frozen=True)
class IntersectionObservation:
    crossing_id: int
    current_phase_id: int
    cycle_second: float
    phases: Dict[int, PhaseObservation]
    downstream_spillback_risk: float
    upstream_release_pressure: float
    offset_error: float


@dataclass(frozen=True)
class NetworkObservation:
    ego: IntersectionObservation
    neighbors: Dict[int, IntersectionObservation]


@dataclass(frozen=True)
class CyclePlan:
    cycle_length: float
    green_times: Dict[int, float]
    phase_order: List[int]
    offset_target: Optional[float] = None


@dataclass(frozen=True)
class PhaseCommand:
    next_phase_id: int
    duration: float
    reason_code: str


class CyclePlannerSkill(Protocol):
    def plan(self, obs: NetworkObservation) -> CyclePlan:
        ...


class PhaseMicroSkill(Protocol):
    def decide(self, obs: NetworkObservation, plan: CyclePlan) -> PhaseCommand:
        ...
```

然后所有进化出来的 Skill 都必须实现同一个接口。

在线执行器只认识这个接口，不认识 LLM：

```python
plan = cycle_skill.plan(network_obs)
plan = safety_layer.clip_cycle_plan(plan, constraints)

command = micro_skill.decide(network_obs, plan)
command = safety_layer.clip_phase_command(command, constraints)

executor.apply(command)
logger.record(obs=network_obs, plan=plan, command=command)
```

---

## 8. 项目目录可以这样重构

基于你现在的仓库，我建议后续改成类似这样：

```text
SignalClawLigh/
  glm_client.py

  sumo_scenarios/
    chengdu/

  signalclaw/
    core/
      state.py
      constraints.py
      skill_api.py
      safety_layer.py
      metrics.py

    adapters/
      sql_loader.py
      sumo_traci_executor.py
      real_signal_executor.py

    skills/
      cycle_planner_v001.py
      phase_micro_v001.py
      registry.py

    evolution/
      llm_mutator.py
      ast_sandbox.py
      evaluator_replay.py
      evaluator_sumo.py
      archive.py
      selector.py
      prompts/

    experiments/
      replay_sql.py
      run_sumo_batch.py
      compare_skills.py

    configs/
      intersections.yaml
      constraints.yaml
      evolution.yaml
```

其中最关键的是：

```text
core/skill_api.py
core/safety_layer.py
adapters/sql_loader.py
adapters/sumo_traci_executor.py
evolution/evaluator_*.py
```

先把这几块做出来，项目就立住了。

---

## 9. SQL 应该怎么接入

你现在最应该从 SQL 里抽象出一个“每周期样本表”或 materialized view。

原始表可能非常大，不建议每次训练都直接扫 `traffic_flow_record`。可以先构建：

```text
cycle_state_view
```

每一行代表：

```text
某个 crossing_id
某个 cycle_id
某个 phase_id
周期开始时间
周期结束时间
相位绿灯开始时间
相位绿灯结束时间
area1_car / area2_car 聚合
预测流量 predicted_value
历史 base_duration
AI change
micro_duration
actual_duration
邻近路口同步状态
后续 1/2/3 个周期的结果指标
```

这样 Skill 离线进化时读的不是 SQL 原始事件流，而是已经整理好的周期级状态。

你仓库文档里已经有这些字段来源：流量来自 `traffic_flow_record`，预测来自 `prediction_record`，AI 调整来自 `analyze_result`，执行链路来自 `batch_plan`、`run_info`、`run_timing_adjustment`，绿波协调来自 `wave`、`wave_node`。([GitHub][2])

---

## 10. 实验方案建议

实验不要一上来就做复杂大模型。建议分三阶段。

### 阶段一：数据对齐

目标：

```text
SQL crossing_id ↔ SUMO tls id
SQL phase_id ↔ SUMO phase index
SQL direction_id / camera_phase_id ↔ SUMO lane / edge
SQL actual_duration ↔ SUMO phase duration
```

产物：

```text
mapping.yaml
cycle_state_view
historical_replay_dataset.parquet
```

### 阶段二：手写 baseline Skill

至少做 4 个 baseline：

```text
fixed_time_skill
historical_ai_replay_skill
local_pressure_skill
neighbor_pressure_skill
```

没有 baseline，不要直接上 LLM 进化。否则你不知道进化结果到底有没有意义。

### 阶段三：LLM 离线进化

每个候选 Skill 评估：

```text
历史回放安全分
SUMO 仿真性能分
多路口协调分
稳定性分
复杂度分
```

推荐指标：

```text
平均延误
最大排队
平均排队
通过量
停车次数
相位饥饿次数
min/max green 违规次数
周期波动
下游溢出风险
邻近路口 offset 偏差
Skill 代码复杂度
```

数据切分建议：

```text
训练：2025-11 到 2026-03
验证：2026-04
测试：2026-05
```

因为文档里说 SQL 数据跨度大约是 2025-11 到 2026-05，这样刚好可以做时间外推测试。([GitHub][2])

---

## 11. 关于 memory

我建议要有 memory，但不是在线大模型 memory。

应该有两种 memory：

### 运行时短记忆

这是普通状态历史，不是 LLM memory：

```text
过去 3 个周期的排队变化
过去 3 个周期的绿灯分配
过去 5 分钟的预测误差
当前路口和邻居的释放节奏
```

它可以作为 Skill 输入。

### 离线进化记忆

这是候选 Skill 档案库：

```text
skill_id
代码
生成 prompt
父代 skill
评估结果
失败案例
适用场景
是否通过安全检查
是否进入线上候选
```

这个非常重要。没有 archive，LLM 会反复生成类似但不可控的代码。

---

## 12. 我建议的技术路线一句话总结

你的项目可以定义成：

> **SignalClawLigh 是一个面向多路口交通信号控制的离线 LLM-guided Skill Evolution 系统。LLM 只负责在离线阶段生成、修复、组合和优化可执行 Skill；在线阶段由确定性 Skill、约束层和执行器完成控灯。系统使用真实 SQL 数据进行历史回放和状态建模，使用 SUMO/TraCI 做数字孪生仿真评估，并通过多目标评估选择可解释、可复现、可安全上线的周期规划 Skill 与相位微调 Skill。**

我下一步最建议你补的是：贴一小段真实 SQL 的 `SHOW CREATE TABLE`，尤其是 `crossing`、`crossing_phase`、`traffic_flow_record`、`prediction_record`、`analyze_result`、`run_timing_adjustment`，我就可以直接把 `cycle_state_view` 和 Skill 输入输出 schema 给你定成可写代码的版本。

[1]: https://github.com/Radar-Lei/SignalClawLigh "GitHub - Radar-Lei/SignalClawLigh: SignalClawLigh project workspace · GitHub"
[2]: https://github.com/Radar-Lei/SignalClawLigh/blob/main/docs/%E4%BA%A4%E5%8F%89%E5%8F%A3%E6%95%B0%E6%8D%AE%E8%AF%B4%E6%98%8E.md "SignalClawLigh/docs/交叉口数据说明.md at main · Radar-Lei/SignalClawLigh · GitHub"
[3]: https://sumo.dlr.de/docs/Tutorials/TraCI4Traffic_Lights.html?utm_source=chatgpt.com "TraCI4Traffic Lights - SUMO Documentation"
[4]: https://github.com/firepd/PI-Light/blob/main/02_run_MCTS.py "PI-Light/02_run_MCTS.py at main · firepd/PI-Light · GitHub"
[5]: https://github.com/firepd/PI-Light/blob/main/agent/pi_light/pi_light.py "PI-Light/agent/pi_light/pi_light.py at main · firepd/PI-Light · GitHub"
[6]: https://github.com/firepd/PI-eLight "GitHub - firepd/PI-eLight · GitHub"
[7]: https://deepmind.google/blog/funsearch-making-new-discoveries-in-mathematical-sciences-using-large-language-models/?utm_source=chatgpt.com "FunSearch: Making new discoveries in mathematical ..."
[8]: https://arxiv.org/abs/2309.03409?utm_source=chatgpt.com "Large Language Models as Optimizers"
