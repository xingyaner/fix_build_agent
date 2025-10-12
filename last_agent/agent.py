import os
import time
import asyncio
import logging
from datetime import datetime
from google.adk.models.lite_llm import LiteLlm
from .util import load_instruction_from_file
from google.adk.agents import LoopAgent, LlmAgent, BaseAgent, SequentialAgent
from google.genai import types
from google.adk.runners import InMemoryRunner
from google.adk.agents.invocation_context import InvocationContext
from google.adk.tools.tool_context import ToolContext
from typing import AsyncGenerator, Optional
from google.adk.events import Event, EventActions
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
    delete_file,
    apply_solution_file,
    run_fuzz_build_streaming
)


class AgentLogger:
    """
    一个全局的、延迟初始化的日志记录器。
    它首先将日志缓存在内存中，然后在获取项目名称后，
    将所有缓存的日志刷入动态命名的文件，并继续记录。
    """

    def __init__(self, log_directory: str = "agent_logs"):
        self.log_directory = log_directory
        self.logger = None
        self.file_handler_setup = False
        self.log_buffer = []  # 用于在文件名确定前缓存日志
        os.makedirs(self.log_directory, exist_ok=True)
        self._log_to_buffer("Logger initialized. Waiting for project name...")

    def _log_to_buffer(self, message: str):
        """将日志消息存入内存缓冲区。"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
        self.log_buffer.append(f"{timestamp} - INFO - {message}")

    def setup_file_handler(self, project_name: str):
        """在获取到项目名称后，配置日志文件并刷入缓存。"""
        if self.file_handler_setup:
            return  # 防止重复设置

        safe_project_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        timestamp = datetime.now().strftime("%Y.%m.%d_%H.%M")
        log_filename = f"{safe_project_name}_{timestamp}.log"
        log_filepath = os.path.join(self.log_directory, log_filename)

        self.logger = logging.getLogger(f"AgentLogger_{safe_project_name}_{timestamp}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        file_handler = logging.FileHandler(log_filepath, encoding='utf-8')
        # 注意：这里的格式化器没有时间戳，因为我们的缓冲区已经带了时间戳
        formatter = logging.Formatter('%(message)s')
        file_handler.setFormatter(formatter)

        if not self.logger.handlers:
            self.logger.addHandler(file_handler)

        print(f"✅ 日志记录器已完全初始化，日志将保存到: {log_filepath}")

        # 将所有缓存的日志一次性写入文件
        for log_entry in self.log_buffer:
            self.logger.info(log_entry)

        self.log_buffer = []  # 清空缓冲区
        self.file_handler_setup = True
        self.logger.info(f"INFO - Flushed buffer. Live logging to file has started.")

    def log_event(self, event: Event):
        """处理并记录 Runner 发出的事件的回调函数。"""
        if not self.file_handler_setup and \
                event.action == EventActions.AGENT_FINISH and \
                event.payload.get('agent_name') == 'initial_setup_agent':

            output = event.payload.get('output', {})
            basic_info = output.get('basic_information', {})
            project_name = basic_info.get('project_name')

            if project_name:
                self.setup_file_handler(project_name)
            else:
                self._log_to_buffer("WARNING - Could not find project_name in initial_setup_agent output.")
                self.setup_file_handler("unknown_project")

        log_message = self._format_message(event)

        if log_message:
            print(log_message)  # 实时打印到控制台
            if self.file_handler_setup:
                # 文件处理器设置好后，直接写入文件
                self.logger.info(f"INFO - {log_message}")
            else:
                # 否则，写入内存缓冲区
                self._log_to_buffer(log_message)

    def _format_message(self, event: Event) -> str:
        """根据事件类型格式化日志消息。"""
        action_map = {
            EventActions.AGENT_START: f"[AGENT START] - Name: {event.payload.get('agent_name')}, Input: {event.payload.get('input', {})}",
            EventActions.AGENT_FINISH: f"[AGENT FINISH] - Name: {event.payload.get('agent_name')}, Output: {event.payload.get('output', {})}",
            EventActions.TOOL_START: f"[TOOL CALL] - Agent: {event.payload.get('agent_name')}, Tool: {event.payload.get('tool_name')}, Args: {event.payload.get('tool_args', {})}",
            EventActions.TOOL_FINISH: f"[TOOL RETURN] - Tool: {event.payload.get('tool_name')}, Output: {event.payload.get('tool_output', {})}",
            EventActions.ERROR: f"[ERROR] - Details: {event.payload.get('details', 'No details')}"
        }
        return action_map.get(event.action, "")


# 创建一个全局唯一的日志记录器实例
# 当 `adk` 导入此文件时，这个实例就会被创建
GLOBAL_LOGGER = AgentLogger()

# --- 猴子补丁开始 ---
# 保存原始的 InMemoryRunner.run 方法
_original_run = InMemoryRunner.run


async def _patched_run(self, *args, **kwargs):
    """我们自己的 run 方法，它会注入 event_callback。"""
    print("--- Patched runner is active. Injecting logger callback. ---")
    # 无论原始调用是否包含 event_callback，我们都强制使用我们的
    kwargs['event_callback'] = GLOBAL_LOGGER.log_event

    # 调用原始的 run 方法，但使用的是我们修改后的参数
    result = await _original_run(self, *args, **kwargs)

    # 如果 Agent 运行极快，可能在回调触发前就结束了，这里确保日志文件被创建
    if not GLOBAL_LOGGER.file_handler_setup:
        GLOBAL_LOGGER.setup_file_handler("fallback_project")

    return result


# 用我们打过补丁的方法替换掉原来的方法
InMemoryRunner.run = _patched_run
# --- 猴子补丁结束 ---


# --- Constants ---
API_DELAY_SECONDS = 40  # 定义延时常量
APP_NAME = "fix_build_agents_v1"
USER_ID = "dev_admin_01"
SESSION_ID_BASE = "loop_exit_tool_session" # New Base Session ID
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

# 日志记录器定义
class AgentLogger:
    """根据项目名称动态创建日志文件的日志记录器。"""

    def __init__(self, log_directory: str = "agent_logs"):
        self.log_directory = log_directory
        self.logger = None
        self.file_handler_setup = False
        os.makedirs(self.log_directory, exist_ok=True)
        print(f"日志目录 '{self.log_directory}' 已准备就绪。")

    def setup_file_handler(self, project_name: str):
        """获取到项目名称后，配置日志文件。"""
        # 替换文件名中可能存在的无效字符
        safe_project_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        timestamp = datetime.now().strftime("%Y.%m.%d_%H.%M")
        log_filename = f"{safe_project_name}_{timestamp}.log"
        log_filepath = os.path.join(self.log_directory, log_filename)

        self.logger = logging.getLogger(f"AgentLogger_{safe_project_name}_{timestamp}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        file_handler = logging.FileHandler(log_filepath, encoding='utf-8')
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)

        if not self.logger.handlers:
            self.logger.addHandler(file_handler)

        self.file_handler_setup = True
        print(f"✅ 日志记录器已完全初始化，日志将保存到: {log_filepath}")

    def log_event(self, event: Event):
        """处理并记录 Runner 发出的事件的回调函数。"""
        if not self.file_handler_setup and \
                event.action == EventActions.AGENT_FINISH and \
                event.payload.get('agent_name') == 'initial_setup_agent':

            output = event.payload.get('output', {})
            basic_info = output.get('basic_information', {})
            project_name = basic_info.get('project_name')

            if project_name:
                self.setup_file_handler(project_name)
            else:
                print("⚠️ 警告: 未能从 initial_setup_agent 的输出中找到 project_name，将使用 'unknown_project'。")
                self.setup_file_handler("unknown_project")

        log_message = self._format_message(event)

        if log_message:
            print(log_message)  # 实时打印到控制台
            if self.logger and self.file_handler_setup:
                self.logger.info(log_message)

    def _format_message(self, event: Event) -> str:
        """根据事件类型格式化日志消息。"""
        if event.action == EventActions.AGENT_START:
            return f"[AGENT START] - Name: {event.payload.get('agent_name')}, Input: {event.payload.get('input', {})}"
        if event.action == EventActions.AGENT_FINISH:
            return f"[AGENT FINISH] - Name: {event.payload.get('agent_name')}, Output: {event.payload.get('output', {})}"
        if event.action == EventActions.TOOL_START:
            return f"[TOOL CALL] - Agent: {event.payload.get('agent_name')}, Tool: {event.payload.get('tool_name')}, Args: {event.payload.get('tool_args', {})}"
        if event.action == EventActions.TOOL_FINISH:
            return f"[TOOL RETURN] - Tool: {event.payload.get('tool_name')}, Output: {event.payload.get('tool_output', {})}"
        if event.action == EventActions.ERROR:
            return f"[ERROR] - Details: {event.payload.get('details', 'No details')}"
        return ""


# 1. 初始设置 Agent (工作流的第一步，只运行一次)
initial_setup_agent = LlmAgent(
    name="initial_setup_agent",
#    model=GEMINI_MODEL,
    model=LiteLlm(model=MODEL,api_key=DPSEEK_API_KEY),
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
    model=LiteLlm(model=MODEL,api_key=DPSEEK_API_KEY),
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
    model=LiteLlm(model=MODEL,api_key=DPSEEK_API_KEY),
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
    model=LiteLlm(model=MODEL,api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("prompt_generate_instruction.txt"),
    description="一个能够保存文件树结构和读写文件内容的prompt书写专家。",
    # --- tools列表包含了所有需要的、从外部导入的工具 ---
    tools=[
        prompt_generate_tool,
        save_file_tree,
        save_file_tree_shallow,
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
    model=LiteLlm(model=MODEL,api_key=DPSEEK_API_KEY),
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
    model=LiteLlm(model=MODEL,api_key=DPSEEK_API_KEY),
    instruction=(
        "你的任务是执行一个文件修改任务。你需要两个信息："
        "1. `solution_file_path`: 修改方案的文件，文件名为 solution.txt，位于当前运行 agent 的目录中。"
        "2. `target_directory`: 需要应用这些修改的项目配置文件的路径，该路径可以从'solution.txt'中获取"
        "获取到这两个信息后，你必须调用 `apply_solution_file` 工具来完成任务，然后向用户报告执行结果。"
    ),
    description="一个能够读取解决方案文件并将其应用到目标项目中的执行代理。",
    tools=[apply_solution_file],
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
    max_iterations=10 # 最大循环轮数
)

root_agent = SequentialAgent(
    name="fix_fuzz_agent",
    sub_agents=[
        initial_setup_agent,
        workflow_loop_agent
    ],
    description="你是一个 Fuzzing 构建修复工作流的助手"
)


