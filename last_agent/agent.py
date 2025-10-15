import os
import time
import json
import asyncio
import logging
from datetime import datetime
from google.genai import types
from typing import AsyncGenerator, Optional
from .util import load_instruction_from_file
from google.adk.runners import InMemoryRunner
from google.adk.models.lite_llm import LiteLlm
from google.adk.events import Event, EventActions
from google.adk.tools.tool_context import ToolContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents import LoopAgent, LlmAgent, BaseAgent, SequentialAgent

# --- 从 agent_tools.py 导入所有需要的工具 ---
from agent_tools import (
    prompt_generate_tool,
    save_file_tree,
    save_file_tree_shallow,
    find_and_append_file_details,
    read_file_content,
    create_or_update_file,
    append_file_to_file,
    append_string_to_file,
    apply_patch,
    delete_file,
    run_fuzz_build_streaming
)


class AgentLogger:
    """
    一个全局的、延迟初始化的日志记录器。
    它能安全地处理快速退出和异常，确保日志始终被保存。
    """

    def __init__(self, log_directory: str = "agent_logs"):
        self.log_directory = log_directory
        self.logger = None
        self.file_handler_setup = False
        self.log_buffer = []
        self.project_name = "unknown_project"
        os.makedirs(self.log_directory, exist_ok=True)
        self._log_to_buffer("Logger initialized. Waiting for project name...")

    def _log_to_buffer(self, message: str):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
        self.log_buffer.append(f"{timestamp} - {message}")

    def setup_file_handler(self):
        if self.file_handler_setup:
            return

        safe_project_name = "".join(c for c in self.project_name if c.isalnum() or c in ('_', '-')).rstrip()
        timestamp = datetime.now().strftime("%Y.%m.%d_%H.%M")
        log_filename = f"{safe_project_name}_project_{timestamp}.log"
        log_filepath = os.path.join(self.log_directory, log_filename)

        self.logger = logging.getLogger(f"AgentLogger_{safe_project_name}_{timestamp}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        file_handler = logging.FileHandler(log_filepath, encoding='utf-8')
        formatter = logging.Formatter('%(message)s')
        file_handler.setFormatter(formatter)

        if not self.logger.handlers:
            self.logger.addHandler(file_handler)

        print(f"✅ Log file created. Path: {log_filepath}")

        for log_entry in self.log_buffer:
            self.logger.info(log_entry)

        self.log_buffer = []
        self.file_handler_setup = True
        self.logger.info(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - INFO - Flushed buffer. Live logging to file has started.")

    def log_event(self, event: Event):
        if not self.file_handler_setup and event.actions and event.actions.state_delta:
            if 'basic_information' in event.actions.state_delta:
                basic_info_str = event.actions.state_delta['basic_information']

                if isinstance(basic_info_str, str):
                    try:
                        # 尝试将字符串解析为字典
                        basic_info = json.loads(basic_info_str)
                        if isinstance(basic_info, dict) and 'project_name' in basic_info:
                            project_name = basic_info['project_name']
                            if project_name:
                                self.project_name = project_name
                            # 成功提取名称后，立即设置文件处理器
                            self.setup_file_handler()
                    except json.JSONDecodeError:
                        # 如果解析失败，则忽略，避免程序崩溃
                        self._log_to_buffer(
                            f"WARNING - Could not parse 'basic_information' JSON string: {basic_info_str}")

        log_message = self._format_message(event)

        if log_message:
            print(log_message)
            if self.file_handler_setup:
                self.logger.info(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {log_message}")
            else:
                self._log_to_buffer(f"INFO - {log_message}")

    def _format_message(self, event: Event) -> str:
        # ... (这个方法保持不变) ...
        author = event.author
        payload = event.payload if hasattr(event, 'payload') else {}
        actions = event.actions
        log_parts = [f"EVENT from author: '{author}'"]
        if hasattr(event, 'get_function_calls') and (func_calls := event.get_function_calls()):
            for call in func_calls:
                log_parts.append(f"  - TOOL_CALL: {call.name}({call.args})")
        if hasattr(event, 'get_function_responses') and (func_resps := event.get_function_responses()):
            for resp in func_resps:
                log_parts.append(f"  - TOOL_RESPONSE for '{resp.name}': {resp.response}")
        if actions:
            if actions.state_delta:
                log_parts.append(f"  - STATE_UPDATE: {actions.state_delta}")
            if actions.escalate:
                log_parts.append("  - ACTION: Escalate (Agent Finish)")
            if actions.transfer_to_agent:
                log_parts.append(f"  - ACTION: Transfer to agent '{actions.transfer_to_agent}'")
        return "\n".join(log_parts)


class LoggingWrapperAgent(BaseAgent):
    """
    这个 Agent 包装了真正的工作流。
    它的 `run` 方法创建了一个受控的内部 Runner，
    并为其附加了我们的全局日志记录器，同时处理异常。
    """
    name: str = "LoggingWrapperAgent"
    subject_agent: BaseAgent

    async def _run_async_impl(self, context: InvocationContext):
        try:
            # 从 subject_agent 获取异步生成器。
            # 这不会立即运行 Agent，只是准备好了事件流。
            agent_event_stream = self.subject_agent.run_async(context)

            # 当 subject_agent 产生事件时，我们在这个循环中捕获它。
            async for event in agent_event_stream:
                # 1. 记录我们刚刚拦截到的事件。
                GLOBAL_LOGGER.log_event(event)

                # 2. 将事件向上 yield，这样主运行器就能正常处理它。
                yield event

        except (Exception, KeyboardInterrupt) as e:
            print(f"\n--- Interruption or Error detected: {type(e).__name__} ---")
            GLOBAL_LOGGER._log_to_buffer(f"ERROR - Agent execution interrupted by {type(e).__name__}: {e}")
            raise e
        finally:
            # 这个块将永远执行，确保日志被保存。
            print("--- Flushing log buffer before exit... ---")
            GLOBAL_LOGGER.setup_file_handler()

        return

# --- 全局日志实例 ---
GLOBAL_LOGGER = AgentLogger()

# --- Constants ---
API_DELAY_SECONDS = 40  # 定义延时常量
APP_NAME = "fix_build_agents_v1"
USER_ID = "dev_admin_01"
SESSION_ID_BASE = "loop_exit_tool_session"  # New Base Session ID
DPSEEK_API_KEY = os.getenv("DPSEEK_API_KEY")
# MODEL = "deepseek/deepseek-v3"
MODEL = "deepseek/deepseek-reasoner"
# GEMINI_MODEL = "gemini-2.5-pro"
# GEMINI_MODEL = "gemini-2.5-flash"
# GEMINI_MODEL = "gemini-2.5-flash-lite"
STATE_INITIAL_TOPIC = "initial_topic"
SUCCESS_PHRASE = "success"

# --- State Keys ---
STATE_PROJECT_NAME = "project_name"
STATE_CONFIG_PATH = "project_config_path"
STATE_SOURCE_PATH = "project_source_path"


# --- 工具定义 ---
def exit_loop(tool_context: ToolContext):
    """构建成功时，由 DecisionAgent 调用此工具，以终止循环。"""
    print(f"[工具调用] exit_loop 工具被 tool_context.agent_name 触发")
    tool_context.actions.escalate = True
    return {"status": "循环因构建成功而退出。"}


def delay() -> str:
    """
    暂停执行固定的40秒，以避免触发 API 速率限制。
    """
    delay_seconds = API_DELAY_SECONDS
    print(f"  [工具调用] delay 工具被调用，将等待 40 秒...")
    time.sleep(delay_seconds)
    print(f"  ...等待结束。")
    return f"Successfully delayed for 40 seconds."


# 1. 初始设置 Agent (工作流的第一步，只运行一次)
initial_setup_agent = LlmAgent(
    name="initial_setup_agent",
    #    model=GEMINI_MODEL,
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction="""
    你是一个负责初始化修复工作流的助手。
    你的任务是：
    1. 从用户的初始请求中收集以下信息并存入 state：
        **项目名称** (例如: 'aiohttp')
        **项目配置文件路径** (例如: '/root/oss-fuzz/projects/aiohttp')
        **项目源码路径** (例如: '/root/fix_build_agent/aiohttp-master')
        **获取项目源码文件树的层数**(作为prompt_generate_agent获取文件树层数时的实参)
    2. 你的最终输出必须是一个 JSON 字符串，包含四个键: "project_name", "project_config_path", "project_source_path","max_depth"。
    用户输入: input
    """,
    output_key="basic_information",
)

# --- Agent 定义 ---
# --- Sub Agent 1: run fuzz and collect log ---
# 通过 run_fuzz_and_collect_log_agent 来获取三个关键信息：**项目名称**, **项目配置文件路径**, **项目源码路径**
run_fuzz_and_collect_log_agent = LlmAgent(
    name="run_fuzz_and_collect_log_agent",
    #    model=GEMINI_MODEL,
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("run_fuzz_and_collect_log_instruction.txt"),
    description="一个能够执行Fuzzing构建命令、捕获错误并自动保存错误日志并实时显示进度的的高级代理。",
    tools=[run_fuzz_build_streaming, create_or_update_file],
    output_key="fuzz_build_log",  # 把结果存入state
)

# --- Sub Agent 2: loop decision ---
# 循环结束条件定义：fuzz_build_log_file/fuzz_build_log.txt 存储的内容为 'success'
decision_agent = LlmAgent(
    name="decision_agent",
    #    model=GEMINI_MODEL,
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction="""
    你是一个构建流程评估员。你的任务是评估构建结果。
    1.  你**必须**首先调用 `read_file_content` 工具，并传入 'fuzz_build_log_file/fuzz_build_log.txt' 来获取构建日志。
    2.  分析该工具返回的内容。
    3.  如果内容**完全等于** "success"：
       你**必须**调用 `exit_loop` 工具，并且不要再做任何事。
    4.  否则（如果内容是错误日志）：
    输出 "继续修复流程。" 这句话，然后**不要**调用任何其他工具。
    """,
    tools=[
        read_file_content,
        exit_loop,
    ],
)
# --- Sub Agent 3: prompt generate ---#####
prompt_generate_agent = LlmAgent(
    name="prompt_generate_agent",
    #    model=GEMINI_MODEL,
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY,max_output_tokens=16384),
    instruction=load_instruction_from_file("prompt_generate_instruction.txt"),
    description="一个能够保存文件树结构和读写文件内容的prompt书写专家。",
    # --- tools列表包含了所有需要的、从外部导入的工具 ---
    tools=[
        prompt_generate_tool,
#        save_file_tree,
#        save_file_tree_shallow,
        find_and_append_file_details,
        read_file_content,
        create_or_update_file,
        append_file_to_file,
        append_string_to_file,
    ],
    output_key="generated_prompt",  # 把结果存入state
)

# --- Sub Agent 4: subject ---
# --- Fuzzing 问题解决 Agent ---
fuzzing_solver_agent = LlmAgent(
    name="fuzzing_solver_agent",
    #    model=GEMINI_MODEL,
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY,max_output_tokens=16384),
    instruction=load_instruction_from_file("fuzzing_solver_instruction.txt"),
    description="一个能够分析fuzzing上下文、生成解决方案并将其保存当前运行 agent 的目录中 'solution.txt' 的专家代理。",
    # 唯一的“行动”就是读取上下文文件。
    tools=[read_file_content, create_or_update_file],
    output_key="solution_plan",  # 把结果存入state
)

# --- Sub Agent 5: content modification ---
solution_applier_agent = LlmAgent(
    name="solution_applier_agent",
    #    model=GEMINI_MODEL,
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY,max_output_tokens=16384),
    instruction=(
        "你是一个精确的代码补丁应用执行官。"
        "你需要从 'solution.txt' 文件中读取补丁内容，solution.txt位于当前运行 agent 的目录中。"
        "**工作流程:**"
        "你**必须**调用 `apply_patch` 工具，并将 `solution_file_path` 参数设置为 'solution.txt'。"
        "**不要**调用其他任何工具"
    ),
    description="一个能够读取补丁文件并将其应用到目标源代码中的执行代理。",
    tools=[read_file_content,apply_patch],
    output_key="basic_information",  # 把结果存入state
)

# workflow loop agent
workflow_loop_agent = LoopAgent(
    name="workflow_loop_agent",
    # 按顺序循环执行
    sub_agents=[
        run_fuzz_and_collect_log_agent,
        decision_agent,
        prompt_generate_agent,
        fuzzing_solver_agent,
        solution_applier_agent,
    ],
    max_iterations=10  # 最大循环轮数
)

subject_agent = SequentialAgent(
    name="fix_fuzz_agent",
    sub_agents=[
        initial_setup_agent,
        workflow_loop_agent
    ],
    description="你是一个 Fuzzing 构建修复工作流的助手"
)

# 创建日志包装器实例，这将是 ADK 的新目标
root_agent = LoggingWrapperAgent(subject_agent=subject_agent)



