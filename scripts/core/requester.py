"""
=============================================================================
模块B：请求执行层 (Request Executor)  —— 完整程序
=============================================================================
【这个文件是干什么的？】
负责跟 GPU 推理服务器（Ollama 或 vLLM）进行网络通信。
核心就三件事：
1. 发请求（把 Prompt 发给模型）
2. 收响应（拿回模型生成的文字）
3. 打时间戳（记录“发出去”和“收回来”的精确时间，用于算性能指标）

【怎么做到高并发？】
全靠 Python 的 asyncio + aiohttp。
整个程序只跑在 1 个线程里，但通过 async/await 语法，
在网络等待时“切出去”做别的任务，实现类似 1000 个线程的效果，且不卡顿。
=============================================================================
"""

import asyncio
import aiohttp
import time
import sys
from pathlib import Path
from typing import List, Dict, Any, Union, Optional

# 将 scripts 目录添加到 Python 模块搜索路径
scripts_dir = Path(__file__).parent.parent  # 当前文件在 scripts/core 下，父目录为 scripts
sys.path.insert(0, str(scripts_dir.absolute()))

# 现在可以导入 scripts 下的 utils 和 core 等
from utils.config_loader import load_config


class RequestExecutor:
    """
    这是“请求执行器”类。
    你把配置文件传进来，它就能帮你发压测请求。
    """

    # ---------------------------------------------------------------------
    # 第一步：初始化（构造函数）
    # 当你写 executor = RequestExecutor(config) 时，这里会被执行
    # ---------------------------------------------------------------------
    def __init__(self, config: Dict[str, Any]):
        """
        初始化执行器。
        输入参数：config —— 就是 config.yaml 解析出来的那个大字典
        输出：无（但会在内部建立网络连接池）
        """

        # ----- 1. 把配置参数存成成员变量（方便后面的函数调用） -----
        self.config = config
        self.api_base = config["api_base_url"].rstrip("/")   # 比如 "http://172.31.34.84:11434"
        self.api_path = config.get("api_path", "/api/generate")  # 默认 /api/generate
        self.framework = config.get("framework", "ollama").lower()  # ollama 或 vllm
        self.model = config.get("model", "deepseek-r1:1.5b")
        self.timeout = config.get("timeout", 30)             # 超时时间 30 秒
        self.stream = config.get("stream", False)            # 压测建议关掉流式
        self.keep_alive = config.get("keep_alive", -1)       # Ollama 专用

        # Ollama 的 options 参数（温度、最大生成数等）
        self.options = {
            "num_predict": config.get("num_predict", 256),
            "temperature": config.get("temperature", 0),
            "num_ctx": config.get("num_ctx", 4096),
            "num_gpu": config.get("num_gpu", 0),   # 新增这一行，默认0表示让Ollama自动分配
        }

        # vLLM 专用参数（虽然名字不同，意义相似）
        self.max_tokens = config.get("num_predict", 256)
        self.temperature_vllm = config.get("temperature", 0)

        # ----- 2. 创建网络连接池（最重要的一步！） -----
        # 解释：以前你用 requests 发请求，每次都要“新建连接 -> 发数据 -> 断开”。
        #       现在 aiohttp 搞了个“连接池”，相当于开了一家餐厅，提前准备了
        #       200 个干净的碗筷（TCP 连接），来客人直接上菜，不用现洗碗。
        #       这在高并发压测下，速度能提升几十倍！
        timeout_config = aiohttp.ClientTimeout(
            total=self.timeout,    # 整个请求最多等 30 秒
            connect=5,             # 建立 TCP 连接最多等 5 秒
            sock_read=self.timeout # 读数据最多等 30 秒
        )
        connector = aiohttp.TCPConnector(
            limit=200,             # 整个程序最多同时保持 200 个连接
            limit_per_host=200,    # 对同一个 IP（172.31.34.84）最多 200 个
            ttl_dns_cache=300      # DNS 缓存 5 分钟
        )
        # 创建会话（相当于餐厅开业）
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout_config,
            headers={"Content-Type": "application/json"}
        )

        # ----- 3. 根据框架选择“请求体构建方法” -----
        # 因为 Ollama 和 vLLM 要求的 JSON 格式不一样，这里做个适配
        if self.framework == "ollama":
            self._build_body = self._build_ollama_body
        elif self.framework == "vllm":
            self._build_body = self._build_vllm_body
            # vLLM 默认路径通常是 /v1/chat/completions
            if self.api_path == "/api/generate":
                self.api_path = "/v1/chat/completions"
        else:
            raise ValueError(f"不支持的框架: {self.framework}，请选 'ollama' 或 'vllm'")

    # ---------------------------------------------------------------------
    # 第二步：内部工具函数（拼装 JSON 请求体）
    # ---------------------------------------------------------------------
    def _build_ollama_body(self, prompt: str) -> Dict[str, Any]:
        """专门给 Ollama 用的：拼成 {"model":..., "prompt":..., "options":...} 格式"""
        return {
            "model": self.model,
            "prompt": prompt,
            "stream": self.stream,
            "options": self.options,
            "keep_alive": self.keep_alive,
        }

    def _build_vllm_body(self, prompt: str) -> Dict[str, Any]:
        """专门给 vLLM 用的：拼成 OpenAI 风格 {"messages":[...], "max_tokens":...}"""
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature_vllm,
            "stream": self.stream,
        }

    # ---------------------------------------------------------------------
    # 第三步：核心！发送单次请求（最关键的函数，包含异步切换机制）
    # 你在伪代码里看到的“打 T1 -> await挂起 -> 等响应 -> 打 T2”全在这里
    # ---------------------------------------------------------------------
    async def send_single_request(self, prompt: str, request_id: Union[int, str]) -> Dict[str, Any]:
        """
        发送 1 个 Prompt 给模型，并返回带着时间戳的原始数据。

        输入：
            prompt       : 用户输入的文本（比如 "你好"）
            request_id   : 这个请求的编号（比如 0, 1, 2 ... 用于日志追踪）

        输出：
            一个字典，里面包含：
            - request_id      : 编号
            - start_timestamp : 发送前那一刻的时间（纳秒）
            - end_timestamp   : 收到响应那一刻的时间（纳秒）
            - rtt_ns          : 往返耗时（end - start）
            - success         : True/False
            - raw_response    : 模型返回的完整 JSON
            - eval_count      : 生成了几 Token
            ... 等等
        """

        # 拼出完整的 URL（比如 http://172.31.34.84:11434/api/generate）
        url = f"{self.api_base}{self.api_path}"
        # 根据框架拼出请求体（调用上面那两个工具函数之一）
        body = self._build_body(prompt)

        # ========== [新增] 发送前检测实际传入的 num_ctx ==========
        # 从 body 中提取 options.num_ctx 打印出来
        actual_num_ctx = body.get("options", {}).get("num_ctx")
        print(f"[调试-发送] 请求ID={request_id}, 本次发送的 num_ctx = {actual_num_ctx}")

        
        # ============ 核心时间戳 T1：发出去之前 ============
        # perf_counter_ns 是电脑里最准的时钟，单位是纳秒（1秒 = 10亿纳秒）
        start_ts = time.perf_counter_ns()

        # 初始化结果字典（先占个位，后面慢慢填）
        result = {
            "request_id": request_id,
            "prompt": prompt,
            "prompt_length": len(prompt),
            "url": url,
            "framework": self.framework,
            "model": self.model,
            "start_timestamp": start_ts,    # T1 已经存进去了
            "end_timestamp": None,          # T2 还没拿到，先空着
            "rtt_ns": None,
            "success": False,
            "status_code": None,
            "error": None,
            "raw_response": None,
            "eval_count": None,
            "total_duration": None,
            "load_duration": None,
            "prompt_eval_count": None,
        }

        try:
            # =============================================================
            # ！关键中的关键！ async with + await 的魔法在这里发生！
            # =============================================================
            # 解释：self.session.post 发起网络请求。
            # 正常情况下，发请求要等服务器算完（可能耗时 1~5 秒）。
            # 如果在“同步代码”里，CPU 就在这里傻等，啥也不干。
            # 
            # 但在“异步代码”里，这个 await 的意思是：
            # “我把数据扔给网卡，然后立刻挂起（暂停）这个任务，
            #   CPU 立刻转头去执行别的请求（比如第 2 个、第 3 个）。
            #   等服务器的数据从网线回来了，操作系统会叫醒我，
            #   我再从这一行下面的代码继续执行。”
            # 
            # 这就是你之前理解的“单线程高速切换任务”的具体实现。
            # =============================================================
            async with self.session.post(url, json=body) as resp:

                # ============ 核心时间戳 T2：收到响应头部的那一瞬间 ============
                # 注意：只要服务器返回了 HTTP 200 状态码，哪怕正文还没读完，
                # 程序就能立刻进到这一行，打上 T2。
                # 这算的是“首包延迟”，非常精准。
                end_ts = time.perf_counter_ns()
                result["end_timestamp"] = end_ts
                result["rtt_ns"] = end_ts - start_ts   # 往返耗时 = T2 - T1
                result["status_code"] = resp.status

                # 如果 HTTP 状态码是 200（成功）
                if resp.status == 200:
                    # 注意：这里又有 await！因为 resp.json() 要读取网络数据流，
                    # 如果正文很大，读取也需要时间。同样，这里也会“挂起”让出 CPU。
                    data = await resp.json()
                    result["success"] = True
                    result["raw_response"] = data

                    # 抽取模型返回的关键指标（Ollama 和 vLLM 字段名不一样，分情况取）
                    if self.framework == "ollama":
                        result["eval_count"] = data.get("eval_count")
                        result["total_duration"] = data.get("total_duration")
                        result["load_duration"] = data.get("load_duration")
                        result["prompt_eval_count"] = data.get("prompt_eval_count")
                    else:  # vLLM
                        usage = data.get("usage", {})
                        result["eval_count"] = usage.get("completion_tokens")
                        result["prompt_eval_count"] = usage.get("prompt_tokens")
                else:
                    # HTTP 4xx 或 5xx 错误
                    error_text = await resp.text()
                    result["error"] = f"HTTP {resp.status}: {error_text[:200]}"

        # 下面是各种异常捕获。不管发生什么（超时、断网、未知错误），
        # 都要把 end_timestamp 补上，保证数据完整，方便模块C统计失败率。
        except asyncio.TimeoutError:
            end_ts = time.perf_counter_ns()
            result["end_timestamp"] = end_ts
            result["rtt_ns"] = end_ts - start_ts
            result["error"] = "TIMEOUT"
        except aiohttp.ClientConnectionError as e:
            end_ts = time.perf_counter_ns()
            result["end_timestamp"] = end_ts
            result["rtt_ns"] = end_ts - start_ts
            result["error"] = f"CONN_REFUSED: {str(e)}"
        except Exception as e:
            end_ts = time.perf_counter_ns()
            result["end_timestamp"] = end_ts
            result["rtt_ns"] = end_ts - start_ts
            result["error"] = f"UNKNOWN: {str(e)}"

        # 把结果字典返回给调用者（就是下面的 batch_execute 函数）
        return result

    # ---------------------------------------------------------------------
    # 第四步：批量执行（模块A会调用这个函数）
    # 在这里实现了“限流器（Semaphore）”，控制同时跑多少个请求
    # ---------------------------------------------------------------------
    async def batch_execute(
        self,
        prompts: List[str],
        concurrency: int,
        request_id_prefix: Optional[str] = ""
    ) -> List[Dict[str, Any]]:
        """
        批量发送 Prompt 列表，限制最大并发数。

        输入：
            prompts            : 一堆 Prompt 的列表，比如 ["你好", "介绍AI", ...]
            concurrency        : 最大并发数（比如 16，表示同时只有 16 个在跑）
            request_id_prefix  : 编号前缀（比如 "round_1"）

        输出：
            一个列表，里面按顺序装着每个请求的原始数据字典。
        """

        # ----- 1. 创建限流器（信号量） -----
        # 想象一下：这个 semaphore 就像一个能坐 concurrency 个人的厕所。
        # 每次只有 concurrency 个人能进去蹲着（执行网络请求），
        # 出来一个，排队的人才能进去一个。
        semaphore = asyncio.Semaphore(concurrency)

        # ----- 2. 定义一个内部嵌套函数（包装一层“抢厕所”的逻辑） -----
        # 注意：这里用了 async def，因为内部还要调用 await send_single_request
        async def _limited_send(prompt: str, idx: int) -> Dict[str, Any]:
            # async with semaphore: 相当于“我要进厕所，如果满了就排队等着”
            async with semaphore:
                # 生成唯一编号（比如 "round_1_5" 表示第1轮的第5个请求）
                req_id = f"{request_id_prefix}_{idx}" if request_id_prefix else str(idx)
                # 最后真正去执行发请求的动作（这里会触发 T1/T2 流程）
                return await self.send_single_request(prompt, req_id)

        # ----- 3. 生成任务清单（此时还没开始执行，只是打包） -----
        # 这一步相当于给每个 Prompt 都贴好了“待办事项”的标签，
        # 但还没交给经理去处理。
        tasks = [
            _limited_send(prompt, i)
            for i, prompt in enumerate(prompts)
        ]

        # ----- 4. 发令枪：asyncio.gather ！！！ -----
        # 这行代码一执行，上面的 tasks 瞬间全部“激活”，
        # 它们会疯狂地争抢 semaphore（厕所）的坑位。
        # 
        # 当坑位满了（比如 16 个），剩下的任务就在 semaphore 的排队区等着。
        # 前面跑完一个，后面立刻补进来一个。
        # 
        # return_exceptions=True 表示：哪怕某个请求报错，也别中断整体，
        # 把错误当成结果返回，免得一个失败拖累全组。
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # ----- 5. 容错处理：把异常对象也转成统一格式的字典 -----
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # 理论上不会发生，因为 send_single_request 已经全捕获了
                final_results.append({
                    "request_id": f"{request_id_prefix}_{i}" if request_id_prefix else str(i),
                    "prompt": prompts[i],
                    "success": False,
                    "error": f"TASK_EXCEPTION: {str(result)}",
                })
            else:
                final_results.append(result)

        # 按顺序返回（索引 0 对应 prompts 的第 0 个）
        return final_results

    # ---------------------------------------------------------------------
    # 第五步：收尾清理（程序结束时必须调用，否则会报警告）
    # ---------------------------------------------------------------------
    async def close(self) -> None:
        """
        关闭 aiohttp 连接池，释放内存和网络端口。
        不关的话，Windows 会报 ResourceWarning，虽然不影响数据，但不优雅。
        """
        if not self.session.closed:
            await self.session.close()


# =========================================================================
# 第六步：测试入口（你可以直接运行这个文件来体验效果）
# 相当于一个小型的 main 函数，用来验证模块能不能正常跑通
# =========================================================================
async def main():
    """
    测试用的主函数。
    模拟了从“加载配置” -> “创建执行器” -> “批量发请求” -> “打印结果” -> “清理”的全流程。
    """
    print("=" * 60)
    print("模块B 独立测试启动（压测请求执行层）")
    print("=" * 60)

    # 1. 加载配置（读取同目录上一级的 config.yaml）
    config = load_config("config.yaml")
    print(f" 配置加载成功")
    print(f" API地址: {config['api_base_url']}")
    print(f" 并发列表: {config['concurrency']}")
    print()

    # 2. 创建执行器（此时内部已经建立连接池）
    executor = RequestExecutor(config)
    print(" 请求执行器初始化完成（连接池已建立）")
    print()

    # 3. 准备测试用的 Prompt（实际项目里从 prompts/ 文件夹读取）
    test_prompts = [
        "你好，请用一句话介绍什么是人工智能。",
        "什么是深度学习？请简要说明。",
        "请解释一下大语言模型的工作原理。"
    ]
    print(f" 测试 Prompt 数量: {len(test_prompts)}")
    print()

    # 4. 调用批量执行（并发数设为 2，方便观察控制台输出）
    print("开始发送请求...")
    results = await executor.batch_execute(test_prompts, concurrency=2)

    # 5. 打印结果摘要
    print("\n 执行结果摘要：")
    success_count = sum(1 for r in results if r.get("success", False))
    print(f"   成功: {success_count} / 总数: {len(results)}")

    for res in results:
        if res["success"]:
            # 把纳秒转成毫秒显示（1毫秒 = 1,000,000 纳秒）
            rtt_ms = res["rtt_ns"] / 1_000_000
            print(f"    [{res['request_id']}] 耗时: {rtt_ms:.2f} ms, "
                  f"生成Token: {res['eval_count']}")
        else:
            print(f"    [{res['request_id']}] 失败: {res['error']}")

    # 6. 收尾清理（必须执行）
    await executor.close()
    print("\n 连接池已关闭，测试结束。")


# -------------------------------------------------------------------------
# 真正的程序起点：当你执行 python request_executor.py 时，这里最先运行
# -------------------------------------------------------------------------
if __name__ == "__main__":
    # asyncio.run(main()) 负责启动“事件循环（大管家）”，
    # 并把 main() 这个异步函数交给管家去跑。
    asyncio.run(main())