import os
import shutil
import time
import json
import re
import asyncio
import subprocess
import litellm
import logging
from datetime import datetime, timedelta
from typing import Dict, AsyncGenerator, Tuple, Optional
from dotenv import load_dotenv

# Load the .env file
load_dotenv()
litellm.request_timeout = 600  # 设置单次请求超时为 10 分钟，防止长代码生成时断连
litellm.num_retries = 2        # litellm 内部针对 500/502/503 错误自动进行 2 次内置重试
litellm.drop_params = True     # 自动过滤模型不支持的参数

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.models.lite_llm import LiteLlm
from google.adk.events import Event
from google.adk.tools.tool_context import ToolContext
from google.adk.agents import LoopAgent, LlmAgent, BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.genai import types
from google.api_core.exceptions import DeadlineExceeded as ContextWindowExceededError

# --- Import all required tools ---
from agent_tools import (
    read_projects_from_yaml,
    update_yaml_report,
    read_projects_from_excel,
    force_clean_git_repo,
    archive_fixed_project,
    download_github_repo,
    get_project_paths,
    checkout_oss_fuzz_commit,
    update_excel_report,
    prompt_generate_tool,
    read_file_content,
    create_or_update_file,
    apply_patch,
    run_command,
    run_fuzz_build_streaming,
    save_file_tree_shallow,
    find_and_append_file_details,
    append_string_to_file,
    get_git_commits_around_date,
    save_commit_diff_to_file,
    update_reflection_journal,
    extract_build_metadata_from_log,
    patch_project_dockerfile,
    truncate_prompt_file
)




# Helper function: Load instruction text from a file
def load_instruction_from_file(filename: str) -> str:
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"Warning: Instruction file '{filename}' not found. The agent will use an empty instruction.")
        return ""

# Logger
class AgentLogger:
    def init(self, log_directory: str = "agent_logs"): self.log_directory=log_directory;self.logger=None;self.file_handler_setup=False;self.log_buffer=[];self.project_name="orchestrator";os.makedirs(self.log_directory,exist_ok=True)
    def set_project_context(self, project_name: str):
        if self.logger:
            for handler in self.logger.handlers[:]: handler.close(); self.logger.removeHandler(handler)
        self.project_name=project_name; self.file_handler_setup=False; self.setup_file_handler()
    def setup_file_handler(self):
        if self.file_handler_setup: return
        safe_project_name="".join(c for c in self.project_name if c.isalnum() or c in ('_','-')).rstrip();timestamp=datetime.now().strftime("%Y.%m.%d_%H.%M.%S");log_filename=f"{safe_project_name}_run_{timestamp}.log";log_filepath=os.path.join(self.log_directory,log_filename);self.logger=logging.getLogger(f"AgentLogger_{safe_project_name}_{timestamp}");self.logger.setLevel(logging.INFO);self.logger.propagate=False;file_handler=logging.FileHandler(log_filepath,encoding='utf-8');formatter=logging.Formatter('%(message)s');file_handler.setFormatter(formatter)
        if not self.logger.handlers: self.logger.addHandler(file_handler)
        print(f"✅ Log file created: {log_filepath}")
        for log_entry in self.log_buffer: self.logger.info(log_entry)
        self.log_buffer=[]
        self.file_handler_setup=True
    def log_event(self, event: Event):
        log_message=self._format_message(event)
        if log_message:
            print(log_message)
            if self.file_handler_setup: self.logger.info(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {log_message}")
            else: self.log_buffer.append(f"INFO - {log_message}")
    def _format_message(self, event: Event) -> str:
        author=event.author;log_parts=[f"EVENT from author: '{author}'"]
        if hasattr(event,'get_function_calls') and (func_calls:=event.get_function_calls()):
            for call in func_calls: log_parts.append(f"  - TOOL_CALL: {call.name}({json.dumps(call.args,ensure_ascii=False)})")
        if hasattr(event,'get_function_responses') and (func_resps:=event.get_function_responses()):
            for resp in func_resps:
                response_str=str(resp.response); response_str=response_str[:500]+"..." if len(response_str)>500 else response_str
                log_parts.append(f"  - TOOL_RESPONSE for '{resp.name}': {response_str}")
        if (actions:=event.actions):
            if actions.state_delta: log_parts.append(f"  - STATE_UPDATE: {actions.state_delta}")
            if actions.escalate: log_parts.append("  - ACTION: Escalate (Agent Finish)")
        return "\n".join(log_parts)

class LoggingWrapperAgent(BaseAgent):
    name: str="LoggingWrapperAgent"
    subject_agent: BaseAgent
    async def _run_async_impl(self, context: InvocationContext) -> AsyncGenerator[Event, None]:
        try:
            async for event in self.subject_agent.run_async(context):
                GLOBAL_LOGGER.log_event(event); yield event
        except (Exception,KeyboardInterrupt) as e: print(f"\n--- Interruption or error detected: {type(e).__name__} ---"); raise e
        finally:
            if not GLOBAL_LOGGER.file_handler_setup: GLOBAL_LOGGER.setup_file_handler()

GLOBAL_LOGGER = AgentLogger()

# Agent Definitions

# --- Constants and Model Configuration ---
APP_NAME = "fix_build_agent_app"
MODEL = "deepseek/deepseek-coder"
DPSEEK_API_KEY = os.getenv("DPSEEK_API_KEY")
USER_ID = "default_user"
MAX_RETRIES = 3

# --- Environment Preparation Agent ---
initial_setup_agent = LlmAgent(
    name="initial_setup_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("instructions/initial_setup__instruction.txt"),
    tools=[
        download_github_repo,
        force_clean_git_repo,
        checkout_oss_fuzz_commit,
        extract_build_metadata_from_log,
        patch_project_dockerfile,
        get_project_paths,
    ],
    output_key="basic_information",
)


# --- Agents in the Fix Loop ---
def exit_loop(tool_context: ToolContext):
    tool_context.actions.escalate = True
    return {"status": "SUCCESS"}

run_fuzz_and_collect_log_agent = LlmAgent(
    name="run_fuzz_and_collect_log_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("instructions/run_fuzz_and_collect_log_instruction.txt"),
    tools=[read_file_content, run_command, run_fuzz_build_streaming, create_or_update_file],
    output_key="fuzz_build_log",
)

decision_agent = LlmAgent(
    name="decision_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("instructions/decision_instruction.txt"),
    tools=[read_file_content, exit_loop],
)

commit_finder_agent = LlmAgent(
    name="commit_finder_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("instructions/commit_finder_instruction.txt"),
    tools=[
        read_projects_from_yaml, 
        read_file_content, 
        get_git_commits_around_date, 
        save_commit_diff_to_file, 
        create_or_update_file,
        run_command,
        get_project_paths # 备用，以防 session 中路径丢失
    ],
    output_key="commit_analysis_result",
)

reflection_agent = LlmAgent(
    name="reflection_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("instructions/reflection_instruction.txt"),
    tools=[read_file_content, update_reflection_journal],
    output_key="last_reflection_result" # 存储工具返回的字典
)

prompt_generate_agent = LlmAgent(
    name="prompt_generate_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=16384),
    instruction=load_instruction_from_file("instructions/prompt_generate_instruction.txt"),
    tools=[prompt_generate_tool, run_command,  save_file_tree_shallow, find_and_append_file_details, read_file_content, create_or_update_file, append_string_to_file],
    output_key="generated_prompt",
)

fuzzing_solver_agent = LlmAgent(
    name="fuzzing_solver_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=16384),
    instruction=load_instruction_from_file("instructions/fuzzing_solver_instruction.txt"),
    tools=[read_file_content, run_command, create_or_update_file],
    output_key="solution_plan",
)

solution_applier_agent = LlmAgent(
    name="solution_applier_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("instructions/solution_applier_instruction.txt"),
    tools=[apply_patch],
    output_key="patch_application_result",
)

summary_agent = LlmAgent(
    name="summary_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("instructions/summary_instruction.txt"),
    tools=[],
    # output_key='.' 会将 Agent 输出的 JSON 对象的每个键值对合并到 state 中，
    # 从而用占位符文本覆盖掉旧的、庞大的状态变量值。
    output_key=".", 
)

# --- Workflow Definition ---
workflow_loop_agent = LoopAgent(
    name="workflow_loop_agent",
    sub_agents=[
        run_fuzz_and_collect_log_agent,
        decision_agent,
        reflection_agent,
        commit_finder_agent,
        prompt_generate_agent,
        fuzzing_solver_agent,
        solution_applier_agent,
        summary_agent,
    ],
    max_iterations=10
)

subject_agent = SequentialAgent(
    name="fix_fuzz_agent",
    sub_agents=[initial_setup_agent, workflow_loop_agent],
    description="A workflow that automatically downloads, configures, and iteratively fixes Fuzzing build issues"
)

root_agent = LoggingWrapperAgent(subject_agent=subject_agent)


def cleanup_environment(project_name: str):
    """
    【精准清理版】
    保留第三方源代码库（process/project/），仅清理日志、中间 Prompt 和修复方案。
    """
    print(f"--- Cleaning up environment (Preserving Source Code) for: {project_name} ---")

    paths_to_remove = [
        "fuzz_build_log_file",
#        "generated_prompt_file", # 包含反思日志，必须在项目切换时清理
#        "solution.txt",
        "file_tree.txt"
    ]

    for path in paths_to_remove:
        if os.path.exists(path):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                print(f"  - Removed: {path}")
            except Exception as e:
                print(f"  - Warning: Failed to remove {path}: {e}")

    # --- 删除了删除 process/project/ 的逻辑 ---
    print(f"--- Cleanup complete. Source code in 'process/project/' has been preserved. ---")


async def process_single_project(
    project_info: Dict,
    session_service: InMemorySessionService
) -> Tuple[bool, Optional[str]]:
    """
    【抗抖动增强版】处理单个项目的完整修复工作流。
    集成了 API 指数退避重试、长上下文冷却时间以及鲁棒的结果解析。
    """
    project_name = project_info['project_name']
    sha = project_info['sha']
    # 关键补全：获取原始日志路径，用于元数据提取
    original_log_path = project_info.get('log_path', "") 

    # 设置日志上下文
    GLOBAL_LOGGER.set_project_context(project_name)

    # 生成唯一的 Session ID
    session_id = f"session_{project_name.replace('-', '_')}_{int(time.time())}"
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    # 关键补全：将日志路径注入初始输入
    initial_input = json.dumps({
        "project_name": project_name, 
        "sha": sha,
        "original_log_path": original_log_path
    })
    initial_message = types.Content(parts=[types.Part(text=initial_input)], role='user')

    is_successful = False
    final_basic_information = None

    # --- 核心重试循环 (处理 API 抖动和 Context 溢出) ---
    for attempt in range(MAX_RETRIES):
        current_session_id = f"session_{project_name.replace('-', '_')}_{int(time.time())}_at{attempt}"
        # 每一轮重试都创建全新的 Session，确保状态纯净
        await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=current_session_id)

        try:
            print(f"\n--- Starting attempt {attempt + 1}/{MAX_RETRIES} for project: {project_name} ---")

            # 【Master Fix】: 注入“喘息时间”与指数退避
            wait_time = 5 if attempt == 0 else (attempt ** 2) * 20
            if attempt > 0:
                print(f"--- [Cooldown] API instability detected. Sleeping for {wait_time}s... ---")
            await asyncio.sleep(wait_time)

            async for event in runner.run_async(user_id=USER_ID, session_id=current_session_id, new_message=initial_message):
                # 实时捕获基础信息（包含路径和提取出的元数据）
                if event.author == 'initial_setup_agent' and event.actions and event.actions.state_delta:
                    if 'basic_information' in event.actions.state_delta:
                        final_basic_information = event.actions.state_delta['basic_information']

                # 实时监测决策 Agent 的成功信号
                if (event.actions and event.actions.escalate and
                    event.author == 'decision_agent' and
                    (resp := event.get_function_responses()) and
                    resp[0].name == 'exit_loop' and resp[0].response.get('status') == 'SUCCESS'):
                    is_successful = True

            break

        except (litellm.exceptions.InternalServerError,
                litellm.exceptions.BadGatewayError,
                litellm.exceptions.ServiceUnavailableError,
                litellm.exceptions.APIConnectionError) as e:
            print(f"\n--- [API SERVER ERROR] Attempt {attempt + 1} failed: {e} ---")
            if attempt + 1 >= MAX_RETRIES:
                print(f"--- [FATAL] Max retries reached for API issues. Giving up on {project_name}. ---")
                return False, None
            continue 

        except ContextWindowExceededError:
            print(f"\n--- [CONTEXT EXCEEDED] Attempt {attempt + 1} failed. ---")
            if attempt + 1 >= MAX_RETRIES:
                print(f"--- [FATAL] Maximum retries reached. Giving up. ---")
                return False, None

            print("--- Attempting to truncate prompt file and retry... ---")
            truncate_prompt_file("generated_prompt_file/prompt.txt")
            await asyncio.sleep(10)
            continue

        except Exception as e:
            print(f"--- [FATAL ERROR] An uncaught exception occurred: {e} ---")
            import traceback
            traceback.print_exc()
            return False, None

    # --- 结果解析逻辑 ---
    project_config_path = None
    if is_successful and final_basic_information:
        if isinstance(final_basic_information, dict):
            project_config_path = final_basic_information.get('project_config_path')
        elif isinstance(final_basic_information, str):
            try:
                match = re.search(r"```json\s*([\s\S]*?)\s*```", final_basic_information, re.MULTILINE)
                if match:
                    info_dict = json.loads(match.group(1))
                    project_config_path = info_dict.get('project_config_path')
                else:
                    info_dict = json.loads(final_basic_information)
                    project_config_path = info_dict.get('project_config_path')
            except Exception as e:
                 print(f"--- [WARNING] Failed to parse config path from info: {e} ---")

    return is_successful, project_config_path


# Main execution logic
async def main():
    """
    【大师级主循环】
    1. 实现了项目间的物理隔离。
    2. 确保环境清理不损害已下载的源代码。
    3. 完善的报告更新与归档机制。
    """
    print("--- Starting automated fix workflow ---")
    GLOBAL_LOGGER.init()

    YAML_FILE = 'projects.yaml'
    session_service = InMemorySessionService()
    
    # 读取待处理项目
    projects_result = read_projects_from_yaml(YAML_FILE)
    if projects_result['status'] == 'error':
        print(f"Error: Could not process YAML file: {projects_result['message']}")
        return
        
    projects_to_process = projects_result.get('projects', [])
    if not projects_to_process:
        print("--- No new projects to process were found. Workflow finished. ---")
        return
        
    print(f"--- Found {len(projects_to_process)} projects to process ---")

    for project_info in projects_to_process:
        project_name = project_info['project_name']
        row_index = project_info['row_index']

        print(f"\n{'='*60}")
        print(f"--- Processing Project: {project_name} (Index: {row_index}) ---")
        print(f"{'='*60}")

        # 【关键步骤 1】: 项目启动前清理
        # 彻底清除上一项目的日志、Prompt、反思记录，但保留 process/project/ 下的源码
        cleanup_environment(project_name)

        # 执行修复流程
        is_successful, project_config_path = await process_single_project(project_info, session_service)

        # 处理修复成功后的逻辑
        if is_successful and project_config_path:
            print(f"--- [SUCCESS] Project fixed. Archiving config from: {project_config_path} ---")
            archive_result = archive_fixed_project(project_name, project_config_path)
            if archive_result['status'] == 'error':
                print(f"--- [CRITICAL] Archiving failed: {archive_result['message']} ---")
        elif is_successful and not project_config_path:
             print("--- [WARNING] Project fixed, but config path missing. Skipping archive. ---")

        # 更新 YAML 状态报告
        result_str = "Success" if is_successful else "Failure"
        print(f"--- Project {project_name} complete. Result: {result_str} ---")
        update_result = update_yaml_report(YAML_FILE, row_index, result_str)

        if update_result['status'] == 'error':
            print(f"--- [CRITICAL] Could not update YAML report: {update_result['message']} ---")

        # 【关键步骤 2】: 项目结束后清理
        # 释放磁盘空间，为下一个项目腾出环境
        cleanup_environment(project_name)

    print("\n--- All projects in the queue have been processed. Workflow finished. ---")




if __name__ == "__main__":
    print("--- Performing pre-startup checks... ---")
    if not DPSEEK_API_KEY:
        print("\n[ERROR] Startup failed: DPSEEK_API_KEY is not set.")
        print("Please do one of the following:")
        print("  - Create a file named '.env' and write: DPSEEK_API_KEY='your_api_key_here'")
        print("  - Or, before running the script, execute: export DPSEEK_API_KEY='your_api_key_here'")
    else:
        print("✅ DPSEEK_API_KEY is set.")
        try:
            subprocess.run(["gh", "--version"], check=True, capture_output=True, text=True)
            print("✅ GitHub CLI ('gh') is installed.")
            subprocess.run(["gh", "auth", "status"], check=True, capture_output=True)
            print("✅ GitHub CLI ('gh') is logged in.")
            print("\n--- Checks complete. Preparing to start the Agent... ---")
            asyncio.run(main())
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print("\n[ERROR] Startup failed: GitHub CLI ('gh') is not installed or not logged in.")
            print("Please install the gh-cli first and authenticate by running 'gh auth login'.")
            print(f"Error details: {e}")
