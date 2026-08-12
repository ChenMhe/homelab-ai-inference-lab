"""
=============================================================================
模块A：控制调度层 (Load Controller)
=============================================================================
职责：
    - 控制测试节奏（轮次、并发、终止）
    - 实现四阶段自动化压测：显存探测 -> 并发探测 -> 正交扫描 -> 长稳验证
    - 内置熔断机制与自动恢复，防止单次OOM导致全盘崩溃
    - 调用模块B（RequestExecutor）执行实际的网络请求
=============================================================================
"""
"""
=============================================================================
模块A 控制调度层 (LoadController) - 命令行调用手册
=============================================================================

【基本用法】
python controller.py [选项]

-----------------------------------------------------------------------------
选项说明
-----------------------------------------------------------------------------
--stage <阶段号>     指定运行一个或多个阶段（逗号分隔）
                      不指定则默认运行全部四个阶段
                      阶段号范围: 1, 2, 3, 4

--interactive        启用交互模式
                      每个阶段结束后等待用户按 Enter 键继续
                      阶段四开始前会额外提示确认（避免意外启动30分钟长测）

-----------------------------------------------------------------------------
常用调用模式示例
-----------------------------------------------------------------------------

1. 全自动运行（默认）
   python controller.py
   说明：按顺序执行阶段一、二、三、四，中间不暂停。
        阶段四将持续运行 30 分钟，请确认服务稳定后再执行。

2. 只运行阶段一（显存探测）
   python controller.py --stage 1
   说明：固定并发=1，逐步增加 Prompt 长度，找到显存极限。
        适合快速确认当前 GPU 能承受的最大上下文长度。

3. 只运行阶段二（并发探测）
   python controller.py --stage 2
   说明：固定长度（阶段一极限的50%），逐步增加并发数，找到计算核心饱和点。
        注意：阶段二依赖阶段一的结果，若未先跑阶段一，会使用config.yaml里的concurrency_test_length自动补充运行。

4. 只运行阶段三（正交扫描）
   python controller.py --stage 3
   说明：在长度和并发的边界内采样 12 个点，绘制性能热力图。
        依赖阶段一和阶段二的结果。

5. 只运行阶段四（长稳测试）
   python controller.py --stage 4
   说明：选择黄金组合，持续满载运行 30 分钟，验证系统稳定性。
        依赖阶段三的正交结果。

6. 交互模式运行全部阶段（推荐初次使用）
   python controller.py --interactive
   说明：每个阶段结束后暂停，等你按 Enter 继续。
        阶段四开始前会特别提示“即将耗时30分钟”，给你反悔的机会。

7. 交互模式只跑阶段一和二
   python controller.py --stage 1,2 --interactive
   说明：阶段一结束后暂停，等你确认后再跑阶段二。
        阶段二结束后直接退出（因为没有后续阶段）。

8. 交互模式跑阶段三和四（跳过前两个阶段）
   python controller.py --stage 3,4 --interactive
   说明：阶段三结束后暂停，确认后进入阶段四。
        注意：跳过阶段一/二时，程序会自动补充运行（但可能耗时较长）。
        若想跳过补充运行，请先单独跑完阶段一/二并记录结果。

-----------------------------------------------------------------------------
交互模式下的控制台提示
-----------------------------------------------------------------------------
[交互] 阶段一（显存探测）已完成。显存极限 = 4096 字符 按 Enter 键继续...
[交互] 阶段二（并发探测）已完成。并发极限 = 16, 饱和RPS = 12.50 按 Enter 键继续...
[交互] 阶段三（正交扫描）已完成。采集到 12 个数据点 按 Enter 键继续...
[交互] 即将开始阶段四（长稳耐力测试），该阶段将持续 30 分钟。
        请确认推理服务已稳定，且你愿意等待。
[交互] 准备就绪 按 Enter 开始长稳测试

可按 Ctrl+C 随时终止程序（阶段四运行中终止会丢失数据）。

-----------------------------------------------------------------------------
阶段依赖关系
-----------------------------------------------------------------------------
阶段一：无依赖。
阶段二：依赖阶段一（若缺失则自动补充运行）。
阶段三：依赖阶段一 + 阶段二（若缺失则报错退出）。
阶段四：依赖阶段三（若缺失则自动从config读取数据）。

建议顺序：先跑阶段一确认显存极限，再跑阶段二确认并发极限，
         再跑阶段三找黄金组合，最后跑阶段四做长稳验证。

-----------------------------------------------------------------------------
测试长度与 Token 说明
-----------------------------------------------------------------------------
- 字符数 vs Token数：模型内部按 Token 计数，字符数只是生成 Token 的原材料。
- 查看真实 Token 数：关注模块A日志中的 [调试] 长度=XXXX 字符, 真实Token数=XXXX。
- 调整测试规模：修改 config.yaml 中的 oom_prompt_lengths 列表。
- 模型上下文上限：使用 ollama show <模型名> 或 API 查询 context_length。

-----------------------------------------------------------------------------


"""


import asyncio
import time
import sys
import os
import statistics
from typing import List, Dict, Any, Optional, Tuple
import csv

# 让 Python 能找到 scripts 目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 修正后的导入：文件名是 requester.py，类名是 RequestExecutor
from core.requester import RequestExecutor
from utils.config_loader import load_config
from utils.prompt_generator import generate_text_by_length


# --------------------------------------------------------------------------
# 辅助统计函数（供模块C预计算使用）
# --------------------------------------------------------------------------
def calculate_rps(results: List[Dict], total_duration_seconds: float) -> float:
    """
    计算本轮测试的每秒请求数（RPS）。
    RPS = 成功请求总数 / 总耗时（秒）
    """
    success_count = sum(1 for r in results if r.get("success", False))
    if total_duration_seconds <= 0:
        return 0.0
    return success_count / total_duration_seconds


def calculate_p99(latencies_ms: List[float]) -> float:
    """
    计算延迟列表的 P99 百分位值。
    输入：毫秒延迟列表
    返回：P99 延迟（毫秒）
    """
    if not latencies_ms:
        return 0.0
    sorted_latencies = sorted(latencies_ms)
    index = int(len(sorted_latencies) * 0.99)
    if index >= len(sorted_latencies):
        index = len(sorted_latencies) - 1
    return sorted_latencies[index]


# --------------------------------------------------------------------------
# 模块A：控制调度层主类
# --------------------------------------------------------------------------
class LoadController:
    """
    压测总指挥官。
    负责按阶段执行测试计划，并处理异常熔断。
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化控制器。
        参数：
            config : 全量配置字典（来自 config.yaml）
        """
        self.config = config
        # 熔断阈值：失败率超过 5% 触发保护
        self.fuse_threshold = 0.05
        # 服务复活等待间隔（秒）
        self.recovery_wait_seconds = 60
        # 最大重试复活次数
        self.max_recovery_attempts = 5

        # 创建模块B的实例（请求执行器）
        self.executor = RequestExecutor(config)

        # 运行时状态标记
        self.is_fuse_triggered = False
        self.skip_test_points = set()  # 记录需要跳过的 (长度, 并发) 组合

        # 测试结果存储
        self.final_report = {}

    # ----------------------------------------------------------------------
    # 阶段零：健康检查与熔断恢复
    # ----------------------------------------------------------------------
    async def _health_check(self) -> bool:
        """
        向推理服务发送极短的探测请求，检查服务是否存活。
        返回：True 表示服务正常，False 表示服务不可用。
        """
        try:
            probe_prompt = "ping"
            results = await self.executor.batch_execute(
                prompts=[probe_prompt],
                concurrency=1,
                request_id_prefix="health"
            )
            if results and results[0].get("success", False):
                return True
            return False
        except Exception:
            return False

    async def _wait_for_recovery(self) -> bool:
        """
        当检测到服务崩溃时，循环等待直到服务重启。
        返回：True 表示服务成功复活，False 表示超时放弃。
        """
        print("[模块A] 警告：推理服务不可用，进入等待恢复模式...")
        for attempt in range(1, self.max_recovery_attempts + 1):
            print(f"[模块A] 恢复尝试 {attempt}/{self.max_recovery_attempts}，等待 {self.recovery_wait_seconds} 秒...")
            await asyncio.sleep(self.recovery_wait_seconds)
            if await self._health_check():
                print("[模块A] 服务已恢复，继续执行测试。")
                return True
        print("[模块A] 错误：服务恢复超时，放弃后续测试。")
        return False

    async def _handle_fuse(self, current_length: int, current_concurrency: int) -> bool:
        """
        熔断处理流程：
            1. 记录导致崩溃的测试点。
            2. 将该点加入跳过列表。
            3. 等待服务重启。
            4. 返回服务是否复活成功。
        """
        print(f"[模块A] 触发熔断保护！长度={current_length}, 并发={current_concurrency}")
        self.skip_test_points.add((current_length, current_concurrency))
        return await self._wait_for_recovery()

    # ----------------------------------------------------------------------
    # [交互] 新增：等待用户按键继续
    # ----------------------------------------------------------------------
    async def _wait_for_user(self, stage_name: str, extra_info: str = ""):
        """
        交互式暂停：打印提示信息，等待用户按下回车键继续。
        参数：
            stage_name : 阶段名称（如 "阶段一：显存探测"）
            extra_info : 附加信息（如 "极限长度=4096"）
        """
        msg = f"\n[交互] {stage_name} 已完成。{extra_info}"
        msg += " 按 Enter 键继续下一阶段，或按 Ctrl+C 终止程序。"
        loop = asyncio.get_event_loop()
        # 使用 run_in_executor 把阻塞的 input() 放到线程池中，不阻塞事件循环
        await loop.run_in_executor(None, input, msg)

    # ----------------------------------------------------------------------
    # 阶段一：单边显存探测（测上下文极限）
    # ----------------------------------------------------------------------
    async def _probe_memory_limit(self) -> int:
        """
        阶段一：探测 KV Cache 显存极限。
        方法：固定并发为 1，采用步进法（从配置的 oom_prompt_lengths 列表中递增）。
        判定：当模块B返回 success=False 时，记录上一个成功的长度作为极限。
        返回：安全的最大上下文长度（字符数）。
        """
        # 读取配置的间隔时间，默认 20 秒
        interval = self.config.get("stage1_interval_seconds", 20)
        print(f"\n[阶段一] 开始探测显存极限（单请求，逐步加压）")
        print(f"[阶段一] 每个测试间隔 {interval} 秒，便于 Prometheus 采集显存曲线")
        concurrency = 1
        length_list = self.config.get("oom_prompt_lengths", [256, 512, 1024, 2048, 4096])
        length_list = sorted(length_list)

        last_success_length = 0
        memory_limit = length_list[-1]

        for idx, length in enumerate(length_list):
            if (length, concurrency) in self.skip_test_points:
                print(f"[阶段一] 跳过已被标记的测试点: 长度={length}")
                continue

            print(f"[阶段一] 正在测试长度: {length} 字符 ...")
            prompt = generate_text_by_length(length)
            print(f"[调试] 生成文本实际长度: {len(prompt)}")   # 确认长度是否等于 length
            # ========== 时间戳 T1：请求发送前 ==========
            send_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            send_time_ns = time.perf_counter_ns()
            print(f"[时间戳] 请求发送: {send_time_str}")

            # 发送请求（修复：删除重复调用）
            results = await self.executor.batch_execute(
                prompts=[prompt],
                concurrency=concurrency,
                request_id_prefix=f"mem_{length}"
            )

            # ========== 时间戳 T2：请求结束后 ==========
            end_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            end_time_ns = time.perf_counter_ns()
            # 计算本次请求耗时（毫秒）
            elapsed_ms = (end_time_ns - send_time_ns) / 1_000_000
            print(f"[时间戳] 请求结束: {end_time_str} (耗时: {elapsed_ms:.0f} ms)")

            result = results[0] if results else {"success": False}

            if result.get("success", False):
                last_success_length = length
                print(f"[阶段一] 长度 {length} 通过。")
                # 打印调试信息（真实 Token 数）
                if result.get("prompt_eval_count"):
                    print(f"[调试] 长度={length} 字符, 真实Token数={result.get('prompt_eval_count')}")
            else:
                error_msg = result.get("error", "未知错误")
                print(f"[阶段一] 长度 {length} 失败，原因: {error_msg[:50]}...")
                recovered = await self._handle_fuse(length, concurrency)
                if recovered:
                    memory_limit = last_success_length
                    print(f"[阶段一] 显存极限确定为: {memory_limit} 字符")
                    return memory_limit
                else:
                    memory_limit = last_success_length if last_success_length > 0 else 0
                    print(f"[阶段一] 服务无法恢复，显存极限确定为: {memory_limit}")
                    return memory_limit

            # ========== 关键改动：每个测试点完成后等待指定间隔 ==========
            # 判断是否还有下一个长度需要测试（避免最后一次测试后无意义等待）
            if idx < len(length_list) - 1:
                print(f"[阶段一] 等待 {interval} 秒后测试下一个长度...")
                await asyncio.sleep(interval)

        # 所有长度都通过，取最后一个成功的长度作为极限
        memory_limit = last_success_length if last_success_length > 0 else length_list[-1]
        print(f"[阶段一] 所有预设长度均通过，显存极限取最大值: {memory_limit}")
        return memory_limit

    # ----------------------------------------------------------------------
    # 阶段二：单边并发探测（测计算核心饱和点）
    # ----------------------------------------------------------------------
    async def _probe_concurrency_limit(self, safe_length: int) -> Tuple[int, float, List, List]:
        """
        阶段二：探测并发极限（计算核心吞吐饱和点）。
        参数：
            safe_length : 阶段一确定的极限长度乘以安全系数（如 0.5）
        返回：
            concurrency_limit : 最大安全并发数（拐点值）
            rps_saturation   : 拐点处的 RPS 值
            rps_history      : 每个并发对应的 RPS 记录 [(并发, RPS), ...]
            p99_history      : 每个并发对应的 P99 记录 [(并发, P99_ms), ...]
        """
        print(f"\n[阶段二] 开始探测并发极限（固定长度={safe_length}，逐步增加并发）")
        concurrency_list = self.config.get("concurrency", [1, 2, 4, 8,])
        rps_history = []
        p99_history = []
        concurrency_limit = concurrency_list[-1]
        rps_saturation = 0.0

        prompt = generate_text_by_length(safe_length)
        requests_per_round = self.config.get("requests_per_round", 50)

        previous_rps = 0.0

        for concurrency in concurrency_list:
            if (safe_length, concurrency) in self.skip_test_points:
                print(f"[阶段二] 跳过已被标记的测试点: 并发={concurrency}")
                continue

            print(f"[阶段二] 正在测试并发数: {concurrency} ...")
            prompts = [prompt] * requests_per_round

            start_time = time.perf_counter()
            results = await self.executor.batch_execute(
                prompts=prompts,
                concurrency=concurrency,
                request_id_prefix=f"con_{concurrency}"
            )
            end_time = time.perf_counter()
            duration = end_time - start_time

            rps = calculate_rps(results, duration)
            latencies_ms = []
            for r in results:
                if r.get("success", False) and r.get("rtt_ns"):
                    latencies_ms.append(r["rtt_ns"] / 1_000_000.0)
            p99 = calculate_p99(latencies_ms)

            rps_history.append((concurrency, rps))
            p99_history.append((concurrency, p99))
            print(f"[阶段二] 并发={concurrency}, RPS={rps:.2f}, P99={p99:.2f}ms")

            if concurrency > 1 and previous_rps > 0:
                growth_rate = (rps - previous_rps) / previous_rps
                if growth_rate < 0.2:
                    concurrency_limit = concurrency_list[concurrency_list.index(concurrency) - 1]
                    rps_saturation = previous_rps
                    print(f"[阶段二] 检测到吞吐饱和拐点，并发极限为: {concurrency_limit}")
                    #break
            await asyncio.sleep(10)
            previous_rps = rps

        if concurrency_limit == concurrency_list[-1]:
            rps_saturation = rps_history[-1][1] if rps_history else 0.0
            print(f"[阶段二] 未在测试范围内发现饱和拐点，取最大并发: {concurrency_limit}")



        return concurrency_limit, rps_saturation, rps_history, p99_history

    # ----------------------------------------------------------------------
    # 阶段三：正交拐点扫描（找最佳性价比组合）
    # ----------------------------------------------------------------------
    async def _orthogonal_scan(self, memory_limit: int, concurrency_limit: int) -> List[Dict]:
        """
        阶段三：正交实验扫描，寻找性能拐点。
        原理：选取 30%、50%、70% 的极限长度 与 30%、50%、70%、100% 的极限并发
              组成 3x4 = 12 个测试点，绘制热力图。
        参数：
            memory_limit      : 阶段一得出的显存极限
            concurrency_limit : 阶段二得出的并发极限
        返回：
            正交实验结果集列表，每个元素包含 {长度, 并发, RPS, P99, 成功率}
        """
        print(f"\n[阶段三] 开始正交拐点扫描（显存极限={memory_limit}, 并发极限={concurrency_limit}）")
        length_points = [
            int(memory_limit * 0.3),
            int(memory_limit * 0.5),
            int(memory_limit * 0.7)
        ]
        concurrency_points = [
            max(1, int(concurrency_limit * 0.3)),
            max(1, int(concurrency_limit * 0.5)),
            max(1, int(concurrency_limit * 0.7)),
            max(1, int(concurrency_limit * 1.0))
        ]
        length_points = sorted(set(length_points))
        concurrency_points = sorted(set(concurrency_points))

        orthogonal_results = []
        requests_per_round = self.config.get("requests_per_round", 50)

        for length in length_points:
            for concurrency in concurrency_points:
                if (length, concurrency) in self.skip_test_points:
                    print(f"[阶段三] 跳过已标记点: 长度={length}, 并发={concurrency}")
                    continue

                print(f"[阶段三] 测试组合: 长度={length}, 并发={concurrency} ...")
                prompt = generate_text_by_length(length)
                prompts = [prompt] * requests_per_round

                start_time = time.perf_counter()
                results = await self.executor.batch_execute(
                    prompts=prompts,
                    concurrency=concurrency,
                    request_id_prefix=f"orth_{length}_{concurrency}"
                )
                end_time = time.perf_counter()
                duration = end_time - start_time

                success_count = sum(1 for r in results if r.get("success", False))
                total_count = len(results)
                success_rate = (success_count / total_count) * 100 if total_count > 0 else 0.0
                rps = calculate_rps(results, duration)

                latencies_ms = []
                for r in results:
                    if r.get("success", False) and r.get("rtt_ns"):
                        latencies_ms.append(r["rtt_ns"] / 1_000_000.0)
                p99 = calculate_p99(latencies_ms)

                record = {
                    "length": length,
                    "concurrency": concurrency,
                    "rps": rps,
                    "p99_ms": p99,
                    "success_rate": success_rate,
                    "success_count": success_count,
                    "total_count": total_count
                }
                orthogonal_results.append(record)
                print(f"[阶段三] 结果: RPS={rps:.2f}, P99={p99:.2f}ms, 成功率={success_rate:.1f}%")

                if success_rate < 95.0:
                    print(f"[阶段三] 组合 长度={length}, 并发={concurrency} 成功率过低，触发熔断。")
                    recovered = await self._handle_fuse(length, concurrency)
                    if not recovered:
                        print("[阶段三] 服务未恢复，正交扫描提前终止。")
                        return orthogonal_results

        print(f"[阶段三] 正交扫描完成，共采集 {len(orthogonal_results)} 个数据点。")
        return orthogonal_results

    # ----------------------------------------------------------------------
    # 阶段四：长稳耐力测试
    # ----------------------------------------------------------------------
    async def _endurance_test(self, orthogonal_results: List[Dict]) -> Dict:
        """
        阶段四：长稳耐力测试。
        从正交结果中自动选取黄金组合（成功率100%且RPS最高）。
        固定该组合持续运行 30 分钟，轮次间隔为 0，满载 GPU。
        内置熔断机制：若累计失败率 > 5% 则触发熔断恢复。
        参数：
            orthogonal_results : 阶段三的正交结果集
        返回：
            长稳测试结果字典，包含总轮次、总成功率、P99抖动、是否通过等
        """
        print("\n[阶段四] 开始长稳耐力测试（持续灌流30分钟）")

        golden_candidates = [r for r in orthogonal_results if r.get("success_rate", 0) == 100.0]
        if not golden_candidates:
            print("[阶段四] 警告：未找到 100% 成功的组合，降级为取成功率最高且 P99 最低的保守组合。")
            sorted_orth = sorted(orthogonal_results, key=lambda x: (-x["success_rate"], x["p99_ms"]))
            golden = sorted_orth[0]
        else:
            golden = max(golden_candidates, key=lambda x: x["rps"])

        golden_length = golden["length"]
        golden_concurrency = golden["concurrency"]
        print(f"[阶段四] 选定黄金组合: 长度={golden_length}, 并发={golden_concurrency}, RPS={golden['rps']:.2f}")

        target_duration_seconds = self.config.get("endurance_duration_seconds", 1800)  # 默认30分钟
        requests_per_round = self.config.get("requests_per_round", 50)  # 默认每轮50个请求
        round_interval = self.config.get("round_interval", 0)  # 默认无间隔

        prompt = generate_text_by_length(golden_length)

        start_time = time.perf_counter()
        round_counter = 0
        total_success = 0
        total_fail = 0
        p99_history = []

        while (time.perf_counter() - start_time) < target_duration_seconds:
            round_counter += 1
            prompts = [prompt] * requests_per_round

            round_start = time.perf_counter()
            results = await self.executor.batch_execute(
                prompts=prompts,
                concurrency=golden_concurrency,
                request_id_prefix=f"endure_{round_counter}"
            )
            round_end = time.perf_counter()
            round_duration = round_end - round_start

            success_count = sum(1 for r in results if r.get("success", False))
            fail_count = len(results) - success_count
            total_success += success_count
            total_fail += fail_count

            latencies_ms = []
            for r in results:
                if r.get("success", False) and r.get("rtt_ns"):
                    latencies_ms.append(r["rtt_ns"] / 1_000_000.0)
            round_p99 = calculate_p99(latencies_ms)
            p99_history.append(round_p99)

            total_requests = total_success + total_fail
            fail_rate = total_fail / total_requests if total_requests > 0 else 0.0

            if round_counter % 10 == 0:
                elapsed = time.perf_counter() - start_time
                print(f"[阶段四] 第 {round_counter} 轮, 已运行 {elapsed/60:.1f} 分钟, "
                      f"当前P99={round_p99:.2f}ms, 累计失败率={fail_rate*100:.2f}%")

            if fail_rate > self.fuse_threshold:
                print(f"[阶段四] 累计失败率 {fail_rate*100:.2f}% 超过阈值 {self.fuse_threshold*100}%，触发熔断。")
                recovered = await self._handle_fuse(golden_length, golden_concurrency)
                if recovered:
                    total_success = 0
                    total_fail = 0
                    print("[阶段四] 熔断恢复完成，继续长稳测试。")
                else:
                    print("[阶段四] 熔断恢复失败，长稳测试终止。")
                    break

            if round_interval > 0:
                await asyncio.sleep(round_interval)

        total_requests = total_success + total_fail
        overall_success_rate = (total_success / total_requests * 100) if total_requests > 0 else 0.0

        p99_jitter = statistics.stdev(p99_history) if len(p99_history) > 1 else 0.0
        p99_range = max(p99_history) - min(p99_history) if p99_history else 0.0

        # ---- 在 endurance_result 字典中添加 ----
        endurance_result = {
            "golden_length": golden_length,
            "golden_concurrency": golden_concurrency,
            "total_rounds": round_counter,
            "total_success": total_success,
            "total_fail": total_fail,
            "overall_success_rate": overall_success_rate,
            "p99_jitter_std": p99_jitter,
            "p99_range_ms": p99_range,
            "is_pass": (overall_success_rate == 100.0) and (p99_range < 200.0),
            # ========== 新增以下两个字段 ==========
            "avg_latency_ms": sum(p99_history) / len(p99_history) if p99_history else 0.0,  # 近似平均延迟
            "token_throughput": (total_success * self.config.get("num_predict", 256)) / (time.perf_counter() - start_time) if total_success > 0 else 0.0,
        }        
        return endurance_result

    # ----------------------------------------------------------------------
    # 主调度入口
    # ----------------------------------------------------------------------
    async def run(self, stages: List[int] = None, interactive: bool = False):
        """
        模块A主入口：支持指定运行一个或多个阶段，并支持交互式暂停。
        参数：
            stages      : 列表，如 [1] 只跑阶段一，[2,3] 跑阶段二和三。
                           默认 None 表示跑全部（[1,2,3,4]）。
            interactive : 布尔值，若为 True，则每个阶段结束后等待用户按回车继续。
        """
        if stages is None:
            stages = [1, 2, 3, 4]

        print("=" * 70)
        print(f"模块A 压测控制调度启动 (阶段: {stages}, 交互模式: {interactive})")
        print("=" * 70)

        # 0. 前置健康检查（所有阶段都依赖服务存活）
        print("[模块A] 执行前置健康检查...")
        if not await self._health_check():
            print("[模块A] 服务未就绪，尝试等待恢复...")
            if not await self._wait_for_recovery():
                print("[模块A] 服务无法启动，测试终止。")
                return {"status": "failed", "reason": "service_unavailable"}

        # 初始化状态变量（用于阶段间复用）
        self.memory_limit = None
        self.concurrency_limit = None
        self.rps_saturation = None
        self.rps_history = None
        self.p99_history = None
        self.orthogonal_results = None
        self.endurance_result = None
        # 打印当前生效的 num_ctx，用于验证配置
        print(f"[调试] 程序读取到的 num_ctx 配置值: {self.executor.options.get('num_ctx')}")
        
        # ---- 阶段一 ----
        if 1 in stages:
            self.memory_limit = await self._probe_memory_limit()
            if self.memory_limit == 0:
                print("[模块A] 显存探测失败，无法继续测试。")
                return {"status": "failed", "reason": "memory_probe_failed"}
            self.final_report["memory_limit_chars"] = self.memory_limit
            # [交互] 阶段一结束后暂停（如果后续还有阶段）
            if interactive and (2 in stages or 3 in stages or 4 in stages):
                await self._wait_for_user(
                    "阶段一（显存探测）",
                    f"显存极限 = {self.memory_limit} 字符"
                )
            else:
                # 如果跳过阶段一但后续阶段需要显存极限值
                # 阶段二可以通过 concurrency_test_length 绕过，阶段三/四必须依赖 memory_limit
                need_memory = False
                if 2 in stages and self.config.get("concurrency_test_length") is None:
                    need_memory = True
                if 3 in stages or 4 in stages:
                    need_memory = True
                if need_memory:
                    print("[模块A] 警告：跳过了阶段一，但后续阶段需要显存极限值。")
                    print("[模块A] 请先单独运行阶段一，或手动在 config 中指定极限值。")
                    return {"status": "failed", "reason": "memory_limit_not_available"}

        # ---- 阶段二 ----
        if 2 in stages:
            if self.memory_limit is None:
                safe_length = self.config.get("concurrency_test_length")
                if safe_length is None:
                    print("[模块A] 阶段一结果缺失，且未指定 concurrency_test_length，自动补充运行阶段一...")
                    self.memory_limit = await self._probe_memory_limit()
                    if self.memory_limit == 0:
                        return {"status": "failed", "reason": "memory_probe_failed"}
                    safe_length = max(1, int(self.memory_limit * 0.5))
                else:
                    print(f"[模块A] 使用配置文件指定的 concurrency_test_length: {safe_length} 字符")
            else:
                safe_length = self.config.get("concurrency_test_length")
                if safe_length is None:
                    safe_length = max(1, int(self.memory_limit * 0.5))
            self.concurrency_limit, self.rps_saturation, self.rps_history, self.p99_history = (
                await self._probe_concurrency_limit(safe_length)
            )
            self.final_report["concurrency_limit"] = self.concurrency_limit
            self.final_report["rps_at_saturation"] = self.rps_saturation
            self.final_report["rps_history"] = self.rps_history
            self.final_report["p99_history"] = self.p99_history
            # [交互] 阶段二结束后暂停（如果后续还有阶段）
            if interactive and (3 in stages or 4 in stages):
                await self._wait_for_user(
                    "阶段二（并发探测）",
                    f"并发极限 = {self.concurrency_limit}, 饱和RPS = {self.rps_saturation:.2f}"
                )

        # ---- 阶段三 ----
        if 3 in stages:
            if self.memory_limit is None or self.concurrency_limit is None:
                print("[模块A] 错误：阶段三依赖阶段一和阶段二的结果，但缺失。")
                return {"status": "failed", "reason": "prerequisite_stage_missing"}
            self.orthogonal_results = await self._orthogonal_scan(self.memory_limit, self.concurrency_limit)
            if not self.orthogonal_results:
                print("[模块A] 正交扫描无有效数据，测试终止。")
                return {"status": "failed", "reason": "orthogonal_no_data"}
            self.final_report["orthogonal_data"] = self.orthogonal_results
            # [交互] 阶段三结束后暂停（如果后续还有阶段四）
            if interactive and 4 in stages:
                await self._wait_for_user(
                    "阶段三（正交扫描）",
                    f"采集到 {len(self.orthogonal_results)} 个数据点"
                )

        # ---- 阶段四 ----
        if 4 in stages:
            # 检查是否有正交结果，如果没有则使用手动指定的黄金组合
            if self.orthogonal_results is None:
                # 从 config 读取手动指定的测试长度，默认为 16000
                golden_length = self.config.get("concurrency_test_length", 16000)
                golden_concurrency = 1  # 你手动测出的最优并发数
                print(f"[阶段四] 未检测到正交结果，使用config指定的组合: 长度={golden_length}, 并发={golden_concurrency}")
                # 构造一个单条正交结果，让 _endurance_test 能够运行
                self.orthogonal_results = [
                    {
                        "length": golden_length,
                        "concurrency": golden_concurrency,
                        "rps": 0.370,      # 可填入你手动测的 RPS
                        "p99_ms": 2782,    # 可填入你手动测的 P99
                        "success_rate": 100.0
                    }
                ]
            else:
                print(f"[阶段四] 使用阶段三的正交结果，共 {len(self.orthogonal_results)} 个组合")
            
            # [交互] 阶段四开始前特别提示
            if interactive:
                print("\n[交互] 即将开始阶段四（长稳耐力测试），该阶段将持续 30 分钟。")
                print("        请确认推理服务已稳定，且你愿意等待。")
                await self._wait_for_user("准备就绪", "按 Enter 开始长稳测试")
            
            self.endurance_result = await self._endurance_test(self.orthogonal_results)
            self.final_report["endurance"] = self.endurance_result
            self.final_report["recommended_config"] = {
                "length": self.endurance_result["golden_length"],
                "concurrency": self.endurance_result["golden_concurrency"]
            }

        # ---- 打印最终摘要 ----
        print("\n" + "=" * 70)
        print("模块A 指定阶段执行完成")
        if self.final_report.get("recommended_config"):
            rec = self.final_report["recommended_config"]
            print(f"推荐生产配置: 长度={rec['length']}, 并发={rec['concurrency']}")
        else:
            print("部分阶段完成，请根据需要继续运行后续阶段。")
        print("=" * 70)

        # 修复：显式标记状态为成功
        self.final_report["status"] = "success"
        
        if self.final_report.get("status") == "success":
            from core.analyzer import analyze
            from core.exporter import export
            analyzed = analyze(self.final_report)
            export(analyzed, self.config.get("output_dir", "./output"))
        return self.final_report

    # ----------------------------------------------------------------------
    # 资源清理
    # ----------------------------------------------------------------------
    async def close(self):
        """关闭内部模块B的连接池"""
        await self.executor.close()


# --------------------------------------------------------------------------
# 独立测试入口
# --------------------------------------------------------------------------
async def main():
    """
    入口函数，支持通过命令行参数指定运行哪些阶段以及是否交互。
    用法：
        python controller.py                       # 运行全部四个阶段（自动模式）
        python controller.py --interactive         # 运行全部阶段，每阶段结束后暂停
        python controller.py --stage 1             # 只运行阶段一（自动）
        python controller.py --stage 1 --interactive  # 只跑阶段一（交互，但阶段一结束后无后续阶段，不会暂停）
        python controller.py --stage 1,2 --interactive  # 跑阶段一和二，阶段一结束后暂停，阶段二结束后结束
    """
    interactive = False
    stages = None

    # 解析命令行参数
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--interactive":
            interactive = True
            i += 1
        elif arg == "--stage" and i + 1 < len(sys.argv):
            parts = sys.argv[i + 1].split(",")
            stages = [int(p.strip()) for p in parts if p.strip().isdigit()]
            for s in stages:
                if s not in [1, 2, 3, 4]:
                    print(f"错误：无效的阶段号 {s}，请输入 1-4 之间的数字。")
                    return
            i += 2
        else:
            print("用法: python controller.py [--stage <阶段号>] [--interactive]")
            print("示例:")
            print("  python controller.py                      # 全部阶段（自动）")
            print("  python controller.py --interactive        # 全部阶段（交互）")
            print("  python controller.py --stage 1            # 仅阶段一")
            print("  python controller.py --stage 1,2 --interactive  # 阶段一和二（交互）")
            return

    config = load_config("config.yaml")
    controller = LoadController(config)

    try:
        report = await controller.run(stages=stages, interactive=interactive)
        if report.get("status") == "success":
            rec = report.get("recommended_config")
            if rec:
                print(f"\n[报告摘要] 推荐配置: 长度={rec['length']}, 并发={rec['concurrency']}")
            if "memory_limit_chars" in report:
                print(f"[报告摘要] 显存极限: {report['memory_limit_chars']} 字符")
            if "concurrency_limit" in report:
                print(f"[报告摘要] 并发极限: {report['concurrency_limit']}")
            if "endurance" in report:
                print(f"[报告摘要] 长稳通过: {report['endurance']['is_pass']}")
        else:
            print(f"\n[报告摘要] 测试失败: {report.get('reason', 'unknown')}")
    finally:
        await controller.close()
        if report.get("status") == "success" and report.get("rps_history"):
            with open("concurrency_results.csv", "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["concurrency", "rps", "p99_ms"])
                rps_history = report.get("rps_history", [])
                p99_history = report.get("p99_history", [])
                for (c, rps), (_, p99) in zip(rps_history, p99_history):
                    writer.writerow([c, rps, p99])
            print("[模块A] 并发测试数据已写入 concurrency_results.csv")


if __name__ == "__main__":
    asyncio.run(main())