我看完后的核心判断是：**这个项目不要做成“在线大模型控灯”，而应该做成“离线大模型进化 Skill + 在线确定性安全执行器”**。在线运行时只执行已经冻结、可审计、可复现的代码 Skill；GLM/LLM 只在离线优化 Skill 时调用。

你的公开仓库目前主要包含 `docs`、`sumo_scenarios/chengdu`、`glm_client.py`、`requirements.txt` 等内容；`glm_client.py` 已经有通过环境变量 `GLM_API_KEY` 调用 GLM 的封装，默认模型是 `glm-5.1`，但还没有完整的交通信号控制执行框架。SUMO 场景在 `sumo_scenarios/chengdu` 下，包含 `chengdu.net.xml`、`chengdu.rou.xml` 和 `chengdu.sumocfg`，配置时间大约是 0 到 3599.75 秒，也就是当前更像一个 1 小时仿真场景。([GitHub][1])

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
