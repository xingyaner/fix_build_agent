import asyncio
import os
import time
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

# --- Constants ---
API_DELAY_SECONDS = 40  # 定义延时常量
APP_NAME = "fix_build_agents_v1"
USER_ID = "dev_admin_01"
SESSION_ID_BASE = "loop_exit_tool_session" # New Base Session ID
DPSEEK_API_KEY = os.getenv("DPSEEK_API_KEY")
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
    delay_seconds = 40
    print(f"  [工具调用] delay 工具被调用，将等待 40 秒...")
    time.sleep(delay_seconds)
    print(f"  ...等待结束。")
    return f"Successfully delayed for 40 seconds."


# 1. 初始设置 Agent (工作流的第一步，只运行一次)
initial_setup_agent = LlmAgent(
    name="initial_setup_agent",
#    model=GEMINI_MODEL,
    model=LiteLlm(model="deepseek/deepseek-reasoner",api_key=DPSEEK_API_KEY),
    instruction="""
    你是一个负责初始化修复工作流的助手。
    你的任务是：
    1. 从用户的初始请求中收集以下三项信息并存入 state：
        **项目名称** (例如: 'aiohttp')
        **项目配置文件路径** (例如: '/root/oss-fuzz/projects/aiohttp')
        **项目源码路径** (例如: '/root/fix_build_agent/aiohttp-master')

    2. 你的最终输出必须是一个 JSON 字符串，包含三个键: "project_name", "project_config_path", "project_source_path"。

    用户输入: input
    
    最后必须执行'delay'工具
    """,
    tools=[delay],
    output_key="basic_information",
)



# --- Agent 定义 ---
# --- Sub Agent 1: run fuzz and collect log ---
# 通过 run_fuzz_and_collect_log_agent 来获取三个关键信息：**项目名称**, **项目配置文件路径**, **项目源码路径**
run_fuzz_and_collect_log_agent = LlmAgent(
    name="run_fuzz_and_collect_log_agent",
#    model=GEMINI_MODEL,
    model=LiteLlm(model="deepseek/deepseek-reasoner",api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("run_fuzz_and_collect_log_instruction.txt"),
    description="一个能够执行Fuzzing构建命令、捕获错误并自动保存错误日志并实时显示进度的的高级代理。",
    tools=[run_fuzz_build_streaming, create_or_update_file, delay],
    output_key="fuzz_build_log",  # 把结果存入state
)

# --- Sub Agent 2: loop decision ---
# 循环结束条件定义：fuzz_build_log_file/fuzz_build_log.txt 存储的内容为 'success'
decision_agent = LlmAgent(
    name="decision_agent",
#    model=GEMINI_MODEL,
    model=LiteLlm(model="deepseek/deepseek-reasoner",api_key=DPSEEK_API_KEY),
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
        delay,
    ],
)
# --- Sub Agent 3: prompt generate ---#####
prompt_generate_agent = LlmAgent(
    name="prompt_generate_agent",
#    model=GEMINI_MODEL,
    model=LiteLlm(model="deepseek/deepseek-reasoner",api_key=DPSEEK_API_KEY),
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
        delay,
    ],
    output_key="generated_prompt",  # 把结果存入state
)

# --- Sub Agent 4: subject ---
# --- Fuzzing 问题解决 Agent ---
fuzzing_solver_agent = LlmAgent(
    name="fuzzing_solver_agent",
#    model=GEMINI_MODEL,
    model=LiteLlm(model="deepseek/deepseek-reasoner",api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("fuzzing_solver_instruction.txt"),
    description="一个能够分析fuzzing上下文、生成解决方案并将其保存当前运行 agent 的目录中 'solution.txt' 的专家代理。",
    # 唯一的“行动”就是读取上下文文件。
    tools=[read_file_content, create_or_update_file, delay],
    output_key="solution_plan",  # 把结果存入state
)

# --- Sub Agent 5: content modification ---
solution_applier_agent = LlmAgent(
    name="solution_applier_agent",
#    model=GEMINI_MODEL,
    model=LiteLlm(model="deepseek/deepseek-reasoner",api_key=DPSEEK_API_KEY),
    instruction=(
        "你的任务是执行一个文件修改任务。你需要两个信息："
        "1. `solution_file_path`: 修改方案的文件，文件名为 solution.txt，位于当前运行 agent 的目录中。"
        "2. `target_directory`: 需要应用这些修改的项目配置文件的路径，该路径可以从'solution.txt'中获取"
        "获取到这两个信息后，你必须调用 `apply_solution_file` 工具来完成任务，然后向用户报告执行结果。"
        "最后必须执行'delay'工具"
    ),
    description="一个能够读取解决方案文件并将其应用到目标项目中的执行代理。",
    tools=[apply_solution_file, delay],
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


