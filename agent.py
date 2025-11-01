# agent.py

import os
import time
import json
import asyncio
import logging
from datetime import datetime
from typing import List, Dict, AsyncGenerator, Optional
from dotenv import load_dotenv

# 导入 ADK 框架中所有需要的组件
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.models.lite_llm import LiteLlm
from google.adk.events import Event, EventActions
from google.adk.tools.tool_context import ToolContext
from google.adk.agents import LoopAgent, LlmAgent, BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext

# 导入 genai 库的 types 模块，用于构建 Message 对象
from google.genai import types

# 在所有代码之前立即执行它，以加载 .env 文件
load_dotenv()

# --- 导入所有需要的工具 ---
from agent_tools import (
    read_projects_from_excel,
    download_github_repo,
    get_project_paths,
    find_sha_for_timestamp,
    checkout_oss_fuzz_commit,
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

# 辅助函数：从文件加载指令文本
def load_instruction_from_file(filename: str) -> str:
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"警告: 指令文件 '{filename}' 未找到。将使用空指令。")
        return ""

class AgentLogger:
    """
    一个全局的、延迟初始化的日志记录器。
    """
    def init(self, log_directory: str = "agent_logs"):
        self.log_directory = log_directory
        self.logger = None
        self.file_handler_setup = False
        self.log_buffer = []
        self.project_name = "orchestrator"
        os.makedirs(self.log_directory, exist_ok=True)
        self._log_to_buffer("Logger initialized. Waiting for project context...")

    def _log_to_buffer(self, message: str):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
        self.log_buffer.append(f"{timestamp} - {message}")

    def setup_file_handler(self):
        if self.file_handler_setup:
            return
        safe_project_name = "".join(c for c in self.project_name if c.isalnum() or c in ('_', '-')).rstrip()
        timestamp = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
        log_filename = f"{safe_project_name}_run_{timestamp}.log"
        log_filepath = os.path.join(self.log_directory, log_filename)
        self.logger = logging.getLogger(f"AgentLogger_{safe_project_name}_{timestamp}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        file_handler = logging.FileHandler(log_filepath, encoding='utf-8')
        formatter = logging.Formatter('%(message)s')
        file_handler.setFormatter(formatter)
        if not self.logger.handlers:
            self.logger.addHandler(file_handler)
        print(f"✅ 日志文件已创建。路径: {log_filepath}")
        for log_entry in self.log_buffer:
            self.logger.info(log_entry)
        self.log_buffer = []
        self.file_handler_setup = True
        self.logger.info(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - INFO - 缓存已清空，实时日志记录已启动。")

    def set_project_context(self, project_name: str):
        """为新的项目运行重置日志文件"""
        if self.logger:
            for handler in self.logger.handlers[:]:
                handler.close()
                self.logger.removeHandler(handler)
        self.project_name = project_name
        self.file_handler_setup = False
        self.setup_file_handler()

    def log_event(self, event: Event):
        log_message = self._format_message(event)
        if log_message:
            print(log_message)
            if self.file_handler_setup:
                self.logger.info(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {log_message}")
            else:
                self._log_to_buffer(f"INFO - {log_message}")

    def _format_message(self, event: Event) -> str:
        author = event.author
        log_parts = [f"EVENT from author: '{author}'"]
        if hasattr(event, 'get_function_calls') and (func_calls := event.get_function_calls()):
            for call in func_calls:
                log_parts.append(f"  - TOOL_CALL: {call.name}({json.dumps(call.args, ensure_ascii=False)})")
        if hasattr(event, 'get_function_responses') and (func_resps := event.get_function_responses()):
            for resp in func_resps:
                response_str = str(resp.response)
                if len(response_str) > 500:
                    response_str = response_str[:500] + "... (truncated)"
                log_parts.append(f"  - TOOL_RESPONSE for '{resp.name}': {response_str}")
        if actions := event.actions:
            if actions.state_delta:
                log_parts.append(f"  - STATE_UPDATE: {actions.state_delta}")
            if actions.escalate:
                log_parts.append("  - ACTION: Escalate (Agent Finish)")
        return "\n".join(log_parts)

class LoggingWrapperAgent(BaseAgent):
    """
    这个 Agent 包装了真正的工作流，用于日志记录和异常处理。
    """
    name: str = "LoggingWrapperAgent"
    subject_agent: BaseAgent

    async def _run_async_impl(self, context: InvocationContext) -> AsyncGenerator[Event, None]:
        try:
            agent_event_stream = self.subject_agent.run_async(context)
            async for event in agent_event_stream:
                GLOBAL_LOGGER.log_event(event)
                yield event
        except (Exception, KeyboardInterrupt) as e:
            print(f"\n--- 检测到中断或错误: {type(e).__name__} ---")
            GLOBAL_LOGGER._log_to_buffer(f"ERROR - Agent 执行被 {type(e).__name__} 中断: {e}")
            raise e
        finally:
            print("--- 退出前刷新日志缓存... ---")
            if not GLOBAL_LOGGER.file_handler_setup:
                GLOBAL_LOGGER.setup_file_handler()

# --- 全局日志实例 ---
GLOBAL_LOGGER = AgentLogger()

# --- 常量与模型配置 ---
APP_NAME = "fix_build_agent_app"
MODEL = "deepseek/deepseek-reasoner"
DPSEEK_API_KEY = os.getenv("DPSEEK_API_KEY")
USER_ID = "default_user"

# ==============================================================================
# 独立的、可复用的项目处理函数
# ==============================================================================

async def process_single_project(project_info: Dict, commits_file_path: str, session_service: InMemorySessionService):
    """
    处理单个项目的完整工作流。
    """
    project_name = project_info['project_name']
    error_date = project_info['date']
    
    safe_project_name = "".join(c for c in project_name if c.isalnum() or c == '_').rstrip('_')
    session_id = f"project_{safe_project_name}_{int(time.time())}"

    print(f"\n{'='*60}")
    print(f"--- 开始处理项目: {project_name} (报错日期: {error_date}) ---")
    print(f"{'='*60}")

    GLOBAL_LOGGER.set_project_context(project_name)

    # 【核心修复】在每次循环中创建全新的 Agent 实例
    
    # 1. 创建环境准备 Agent
    environment_setup_agent = LlmAgent(
        name="environment_setup_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
        instruction="""
        你是一个自动化环境配置专家。
        你的输入是一个包含 "project_name", "error_date", "commits_file_path" 的 JSON 字符串。
        请解析这个 JSON，并提取这些值。
        然后按照以下工作流程执行任务:
        1.  调用 `download_github_repo` 工具，`project_name` 参数设置为 "oss-fuzz"。
        2.  调用 `find_sha_for_timestamp` 工具，使用提取出的 `error_date` 和 `commits_file_path` 来找到正确的 commit SHA。
        3.  调用 `checkout_oss_fuzz_commit` 工具，使用 `oss-fuzz` 的路径和上一步找到的 `sha` 作为参数。
        4.  调用 `download_github_repo` 工具，使用提取出的 `project_name` 作为参数。
        5.  【重要】调用 `get_project_paths` 工具，使用提取出的 `project_name` 作为参数。
        6.  将 `get_project_paths` 工具返回的完整 JSON 对象作为你的最终输出。
        """,
        tools=[
            download_github_repo,
            find_sha_for_timestamp,
            checkout_oss_fuzz_commit,
            get_project_paths,
        ],
        output_key="basic_information",
    )

    # 2. 创建修复循环中的 Agents
    def exit_loop(tool_context: ToolContext):
        tool_context.actions.escalate = True
        return {"status": "循环因构建成功而退出。"}

    run_fuzz_and_collect_log_agent = LlmAgent(
        name="run_fuzz_and_collect_log_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
        instruction="""
        你是一个能够执行Fuzzing构建命令的代理。
        你将从会话状态中获取 "project_name" 和 "project_config_path"。
        工作流程:
        1. 调用 `run_fuzz_build_streaming` 工具。
        2. `project_name` 参数使用会话状态中的 "project_name"。
        3. `oss_fuzz_path` 参数的值是 `project_config_path` 中 "/projects/" 之前的部分。
        4. 【重要】在调用工具时，你必须提供 `sanitizer`, `engine`, `architecture` 这三个参数，请使用以下值：
           - sanitizer: "address"
           - engine: "libfuzzer"
           - architecture: "x86_64"
        """,
        tools=[read_file_content, run_fuzz_build_streaming, create_or_update_file],
        output_key="fuzz_build_log",
    )

    decision_agent = LlmAgent(
        name="decision_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
        instruction="""
        你是一个构建流程评估员。你的任务是评估构建结果。
        你必须首先调用 read_file_content 工具，并传入 'fuzz_build_log_file/fuzz_build_log.txt' 来获取构建日志。
        分析该工具返回的内容。
        如果内容完全等于 "success"：
        你必须调用 exit_loop 工具，并且不要再做任何事。
        否则（如果内容是错误日志）：
        输出 "继续修复流程。" 这句话，然后不要调用任何其他工具。
        """,
        tools=[read_file_content, exit_loop],
    )

    prompt_generate_agent = LlmAgent(
        name="prompt_generate_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=16384),
        instruction=load_instruction_from_file("instructions/prompt_generate_instruction.txt"),
        tools=[
            prompt_generate_tool, save_file_tree, save_file_tree_shallow,
            find_and_append_file_details, read_file_content, create_or_update_file,
            append_file_to_file, append_string_to_file,
        ],
        output_key="generated_prompt",
    )

    fuzzing_solver_agent = LlmAgent(
        name="fuzzing_solver_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=16384),
        instruction=load_instruction_from_file("instructions/fuzzing_solver_instruction.txt"),
        tools=[read_file_content, create_or_update_file],
        output_key="solution_plan",
    )

    solution_applier_agent = LlmAgent(
        name="solution_applier_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=16384),
        instruction=(
            "你是一个精确的代码补丁应用执行官。"
            "你需要从 'solution.txt' 文件中读取补丁内容，solution.txt位于当前运行 agent 的目录中。"
            "工作流程:"
            "你必须调用 apply_patch 工具，并将 solution_file_path 参数设置为 'solution.txt'。"
            "不要调用其他任何工具,不要做任何超出任务的事情。"
        ),
        tools=[read_file_content, apply_patch],
        output_key="patch_application_result",
    )

    workflow_loop_agent = LoopAgent(
        name="workflow_loop_agent",
        sub_agents=[
            run_fuzz_and_collect_log_agent,
            decision_agent,
            prompt_generate_agent,
            fuzzing_solver_agent,
            solution_applier_agent,
        ],
        max_iterations=10
    )

    # 3. 构建本次运行的完整工作流
    single_project_workflow = SequentialAgent(
        name="single_project_workflow",
        sub_agents=[
            environment_setup_agent,
            workflow_loop_agent
        ]
    )

    # 4. 准备要通过 new_message 传递的数据
    initial_data = {
        "project_name": project_name,
        "error_date": error_date,
        "commits_file_path": commits_file_path
    }
    message_content = json.dumps(initial_data)
    message_object = types.Content(parts=[types.Part(text=message_content)], role='user')

    # 5. 创建 Runner 并执行
    root_agent = LoggingWrapperAgent(subject_agent=single_project_workflow)
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)
    
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    
    async for _ in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=message_object):
        pass

    print(f"\n--- 项目 {project_name} 处理完毕。---\n")
    time.sleep(5)

# ==============================================================================
# 主执行逻辑
# ==============================================================================

async def main():
    """
    主执行函数，负责编排：获取输入 -> 获取任务列表 -> 分发任务。
    """
    print("--- 启动 Agent 工作流 ---")
    GLOBAL_LOGGER.init()

    session_service = InMemorySessionService()

    # --- 阶段 1: 从用户处获取 Commit 历史文件路径 ---
    print("\n--- 阶段 1: 收集用户提供的 Commit 历史文件路径 ---")
    commits_file_path_input = input("请输入 oss-fuzz 的 commit 历史文件的绝对路径: ")
    if not os.path.exists(commits_file_path_input):
        print(f"--- 错误: 文件路径 '{commits_file_path_input}' 不存在。程序终止。 ---")
        return

    user_input_collector_agent = LlmAgent(
        name="UserInputCollectorAgent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
        instruction="""
        你是一个负责初始化配置的助手。
        你的输入是一个 JSON 字符串
        请解析这个 JSON，并提取 "user_prompt" 字段的值。
        你的最终输出必须是一个 JSON 字符串，包含一个键: "commits_file_path"，其值为你提取到的路径。
        """,
        output_key="initial_config"
    )
        
    user_input_data = {"user_prompt": commits_file_path_input}
    message_content = json.dumps(user_input_data)
    initial_message = types.Content(parts=[types.Part(text=message_content)], role='user')
    
    user_input_runner = Runner(agent=user_input_collector_agent, app_name=APP_NAME, session_service=session_service)
    
    session_id = f"user-input-session-{int(time.time())}"
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    
    async for _ in user_input_runner.run_async(user_id=USER_ID, session_id=session_id, new_message=initial_message):
        pass
    
    try:
        current_session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        initial_config_str = current_session.state.get('initial_config', '{}')
        commits_file_path = json.loads(initial_config_str).get('commits_file_path')
        if not commits_file_path: raise ValueError("未能从 Agent 输出中解析出路径")
        print(f"--- 成功获取 Commit 历史文件路径: {commits_file_path} ---")
    except (json.JSONDecodeError, AttributeError, ValueError) as e:
        print(f"--- 错误: UserInputCollectorAgent 未能成功提取文件路径。工作流终止。错误: {e} ---")
        return

    # --- 阶段 2: 使用工具直接读取 Excel ---
    print("\n--- 阶段 2: 从 Excel 读取项目列表 ---")
    result = read_projects_from_excel('reproduce_report.xlsx')
    
    if result['status'] == 'error':
        print(f"--- 错误: 无法读取 Excel 文件。详情: {result['message']} ---")
        return
        
    projects_to_run = result.get('projects', [])
    if not projects_to_run:
        print("--- 信息: 在 'reproduce_report.xlsx' 中没有找到需要处理的项目。工作流结束。 ---")
        return

    # --- 阶段 3: 循环调用项目处理函数 ---
    print(f"\n--- 阶段 3: 成功获取到 {len(projects_to_run)} 个待处理项目，即将开始处理 ---")
    for project_info in projects_to_run:
        try:
            await process_single_project(project_info, commits_file_path, session_service)
        except Exception as e:
            import traceback
            print(f"\n{'!'*60}")
            print(f"--- 严重错误: 处理项目 {project_info['project_name']} 时发生未捕获的异常 ---")
            print(f"--- 错误类型: {type(e).__name__}")
            print(f"--- 错误详情: {e}")
            print("--- 堆栈跟踪:")
            traceback.print_exc()
            print(f"--- 将跳过此项目，继续处理下一个。")
            print(f"{'!'*60}\n")
            continue

    print("--- 所有项目处理完毕。工作流正常结束。 ---")

if __name__ == "__main__":
    # 检查 API 密钥
    if not DPSEEK_API_KEY:
        print("错误：请在运行前设置 DPSEEK_API_KEY 环境变量或将其写入 .env 文件。")
    else:
        asyncio.run(main())
