import os
import shutil
import time
import json
import re
import asyncio
import subprocess
import litellm
import logging
from datetime import datetime,timedelta
from typing import Dict, AsyncGenerator, Tuple, Optional
from dotenv import load_dotenv

# Load the .env file
load_dotenv()

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
    instruction="""
    You are an automated environment configuration expert. Strictly follow these steps:
    1.  Parse "project_name" and "sha" from the input.
    2.  Call the `download_github_repo` tool to download "oss-fuzz". The `target_dir` must be "./oss-fuzz".
    3.  Call the `force_clean_git_repo` tool, passing the string "./oss-fuzz" as the `repo_path` parameter to ensure the repository is clean.
    4.  Call the `checkout_oss_fuzz_commit` tool, using the parsed `sha` to revert to the specified version.
    5.  Call the `download_github_repo` tool to download the current project. Pass the project name you parsed from the input as the `project_name` parameter, and a new string concatenated from "./process/project/" and the parsed project name as the `target_dir` parameter.
    6.  Call the `get_project_paths` tool, using the parsed `project_name` to generate standard paths.
    7.  Use the return result of the `get_project_paths` tool as your final output.
    """,
    tools=[
        download_github_repo,
        force_clean_git_repo,
        checkout_oss_fuzz_commit,
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
        "generated_prompt_file", # 包含反思日志，必须在项目切换时清理
        "solution.txt",
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
    Processes the complete workflow for a single project, including retries and exception handling.
    Returns a tuple: (was_successful, project_config_path_on_success)
    """
    project_name = project_info['project_name']
    sha = project_info['sha']

    GLOBAL_LOGGER.set_project_context(project_name)

    session_id = f"session_{project_name.replace('-', '_')}_{int(time.time())}"

    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    initial_input = json.dumps({"project_name": project_name, "sha": sha})
    initial_message = types.Content(parts=[types.Part(text=initial_input)], role='user')

    is_successful = False
    final_basic_information = None

    # --- Core Retry Loop ---
    for attempt in range(MAX_RETRIES):
        # Create a new session for each retry to ensure a clean environment
        await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)

        try:
            print(f"--- Starting to process {project_name}, attempt {attempt + 1}/{MAX_RETRIES} ---")

            async for event in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=initial_message):
                # Capture 'basic_information' in real-time for later archiving
                if event.author == 'initial_setup_agent' and event.actions and event.actions.state_delta:
                    if 'basic_information' in event.actions.state_delta:
                        final_basic_information = event.actions.state_delta['basic_information']

                # Check for success signal in real-time
                if (event.actions and event.actions.escalate and
                    event.author == 'decision_agent' and
                    (resp := event.get_function_responses()) and
                    resp[0].name == 'exit_loop' and resp[0].response.get('status') == 'SUCCESS'):
                    is_successful = True

            # If successful, or if the agent loop finishes normally (reaches max_iterations), break the retry loop
            break

        except ContextWindowExceededError:
            print(f"\n--- WARNING: Attempt {attempt + 1}/{MAX_RETRIES} failed due to context window exceeded. ---")
            if attempt + 1 >= MAX_RETRIES:
                print(f"--- ERROR: Maximum retry limit reached. Giving up on project {project_name}. ---")
                return False, None

            print("--- Attempting to truncate the prompt file and retry... ---")
            truncate_prompt_file("generated_prompt_file/prompt.txt")
            await asyncio.sleep(5)
            # continue will automatically proceed to the next for loop iteration

        except Exception as e:
            print(f"--- FATAL ERROR: An uncaught exception occurred while processing project {project_name}: {e} ---")
            import traceback
            traceback.print_exc()
            # On unknown fatal error, return failure directly without retrying
            return False, None

    # --- After the loop, parse the final result and return ---
    project_config_path = None
    if is_successful and final_basic_information:
        info_str = ""
        if isinstance(final_basic_information, str):
            info_str = final_basic_information
        elif isinstance(final_basic_information, dict):
            project_config_path = final_basic_information.get('project_config_path')

        if not project_config_path and info_str:
            try:
                # 1. Try to find a ```json ... ``` code block with regex
                match = re.search(r"```json\s*([\s\S]*?)\s*```", info_str, re.MULTILINE)
                if match:
                    json_str = match.group(1)
                    info_dict = json.loads(json_str)
                    project_config_path = info_dict.get('project_config_path')
                else:
                    # 2. If no code block, try to load the entire string directly
                    try:
                        info_dict = json.loads(info_str)
                        project_config_path = info_dict.get('project_config_path')
                    except json.JSONDecodeError:
                        # 3. If direct loading fails, try to extract content between the first '{' and the last '}'
                        start = info_str.find('{')
                        end = info_str.rfind('}')
                        if start != -1 and end != -1 and start < end:
                            json_str = info_str[start:end+1]
                            info_dict = json.loads(json_str)
                            project_config_path = info_dict.get('project_config_path')
            except (json.JSONDecodeError, AttributeError, IndexError) as e:
                 print(f"--- WARNING: Failed to parse 'basic_information' to get config path. Error: {e} ---")

    return is_successful, project_config_path

# Main execution logic
async def main():
    print("--- Starting automated fix workflow ---")
    GLOBAL_LOGGER.init()

    YAML_FILE = 'projects.yaml'
    session_service = InMemorySessionService()
    projects_result = read_projects_from_yaml(YAML_FILE)

    if projects_result['status'] == 'error':
        print(f"Error: Could not process YAML file: {projects_result['message']}")
        return
    projects_to_process = projects_result.get('projects', [])
    if not projects_to_process:
        print("--- No new projects to process were found in the YAML file. Workflow finished. ---")
        return
    print(f"--- Found {len(projects_to_process)} projects to process ---")

    for project_info in projects_to_process:
        project_name = project_info['project_name']
        row_index = project_info['row_index']

        print(f"\n{'='*60}")
        print(f"--- Starting to process project: {project_name} (Index: {row_index}) ---")
        print(f"{'='*60}")

        # 【MASTER FIX 1】: 项目启动前预清理
        # 确保即使上次运行异常中断，本轮运行的初始环境（尤其是反思日志）是纯净的
        cleanup_environment(project_name)

        # 执行完整的 Agent 修复流程（包含内部最多 10 轮的 LoopAgent 迭代）
        is_successful, project_config_path = await process_single_project(project_info, session_service)

        # 处理修复成功后的归档逻辑
        if is_successful and project_config_path:
            print(f"--- Project successfully fixed. Archiving configuration files from: {project_config_path} ---")
            archive_result = archive_fixed_project(project_name, project_config_path)
            if archive_result['status'] == 'error':
                print(f"--- CRITICAL WARNING: Archiving failed! Error: {archive_result['message']} ---")
        elif is_successful and not project_config_path:
             print("--- WARNING: Project was fixed successfully, but the project config path could not be retrieved. Skipping archive. ---")

        # 更新 YAML 报告
        result_str = "Success" if is_successful else "Failure"
        print(f"--- Project {project_name} processing complete. Result: {result_str} ---")
        update_result = update_yaml_report(YAML_FILE, row_index, result_str)

        if update_result['status'] == 'error':
            print(f"--- CRITICAL WARNING: Could not write result back to YAML file! Error: {update_result['message']} ---")

        # 【MASTER FIX 2】: 项目结束后彻底清理
        # 在进入下一个 project 循环前，清空当前项目的源码、反思日志和临时 Prompt
        cleanup_environment(project_name)

    print("\n--- All projects have been processed. Workflow finished normally. ---")

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
