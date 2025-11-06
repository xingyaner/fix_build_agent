# agent.py (最终修正版)

import os
import shutil
import time
import json
import re
import asyncio
import subprocess
import logging
from datetime import datetime
from typing import Dict, AsyncGenerator, Tuple, Optional
from dotenv import load_dotenv

# 在所有代码之前立即执行，以加载 .env 文件
load_dotenv()

# 导入 ADK 框架
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.models.lite_llm import LiteLlm
from google.adk.events import Event
from google.adk.tools.tool_context import ToolContext
from google.adk.agents import LoopAgent, LlmAgent, BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.genai import types

# --- 导入所有需要的工具 ---
from agent_tools import (
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
    append_string_to_file
)

# 辅助函数：从文件加载指令文本
def load_instruction_from_file(filename: str) -> str:
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"警告: 指令文件 '{filename}' 未找到。Agent 将使用空指令。")
        return ""

# ==============================================================================
# 日志记录器 (保持不变)
# ==============================================================================
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
        print(f"✅ 日志文件已创建: {log_filepath}")
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
        except (Exception,KeyboardInterrupt) as e: print(f"\n--- 检测到中断或错误: {type(e).__name__} ---"); raise e
        finally:
            if not GLOBAL_LOGGER.file_handler_setup: GLOBAL_LOGGER.setup_file_handler()

GLOBAL_LOGGER = AgentLogger()

# ==============================================================================
# Agent 定义
# ==============================================================================

# --- 常量与模型配置 ---
APP_NAME = "fix_build_agent_app"
MODEL = os.getenv("MODEL_NAME", "deepseek/deepseek-reasoner")
DPSEEK_API_KEY = os.getenv("DPSEEK_API_KEY") # 现在可以正确加载了
USER_ID = "default_user"
MAX_RETRIES = 3

# --- 环境准备 Agent ---
# (此 Agent 定义保持不变，它是正确的)
initial_setup_agent = LlmAgent(
    name="initial_setup_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction="""
    你是一个自动化环境配置专家，严格按照以下步骤操作：
    1.  从输入中解析出 "project_name" 和 "sha"。
    2.  调用 `download_github_repo` 工具下载 "oss-fuzz"。`target_dir` 必须是 "./oss-fuzz"。
    3.  调用 `force_clean_git_repo` 工具，为其 `repo_path` 参数传入字符串 "./oss-fuzz"，以确保仓库是干净的。
    4.  调用 `checkout_oss_fuzz_commit` 工具，使用解析出的 `sha` 进行版本回退。
    5.  调用 `download_github_repo` 工具下载当前项目。为其 `project_name` 参数传入你从输入中解析出的项目名，为其 `target_dir` 参数传入一个由 "./process/project/" 和你解析出的项目名拼接而成的新字符串。
    6.  调用 `get_project_paths` 工具，使用你解析出的 `project_name` 来生成标准路径。
    7.  将 `get_project_paths` 工具的返回结果作为你的最终输出。
    """,
    tools=[
        download_github_repo,
        force_clean_git_repo, 
        checkout_oss_fuzz_commit,
        get_project_paths,
    ],
    output_key="basic_information",
)


# --- 修复循环中的 Agents ---
def exit_loop(tool_context: ToolContext):
    tool_context.actions.escalate = True
    return {"status": "SUCCESS"}

# (以下 Agent 定义保持不变，但请确保对应的 instruction 文件存在)
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

prompt_generate_agent = LlmAgent(
    name="prompt_generate_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=16384),
    instruction=load_instruction_from_file("instructions/prompt_generate_instruction.txt"),
    tools=[prompt_generate_tool, save_file_tree_shallow, find_and_append_file_details, read_file_content, create_or_update_file, append_string_to_file],
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

# --- 工作流定义 ---
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

subject_agent = SequentialAgent(
    name="fix_fuzz_agent",
    sub_agents=[initial_setup_agent, workflow_loop_agent],
    description="一个自动下载、配置并循环修复 Fuzzing 构建问题的工作流"
)

root_agent = LoggingWrapperAgent(subject_agent=subject_agent)


async def process_single_project(
    project_info: Dict, 
    session_service: InMemorySessionService
) -> Tuple[bool, Optional[str]]:
    """
    处理单个项目的完整工作流，包含重试和异常处理。
    返回一个元组: (是否成功, 成功时的项目配置路径)
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

    # --- 核心重试循环 ---
    for attempt in range(MAX_RETRIES):
        # 每次重试都创建一个新的会话，以确保环境是干净的
        await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        
        try:
            print(f"--- 开始处理 {project_name}, 第 {attempt + 1}/{MAX_RETRIES} 次尝试 ---")
            
            async for event in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=initial_message):
                # 实时捕获 'basic_information' 以便后续归档
                if event.author == 'initial_setup_agent' and event.actions and event.actions.state_delta:
                    if 'basic_information' in event.actions.state_delta:
                        final_basic_information = event.actions.state_delta['basic_information']
                
                # 实时检查成功信号
                if (event.actions and event.actions.escalate and
                    event.author == 'decision_agent' and
                    (resp := event.get_function_responses()) and
                    resp[0].name == 'exit_loop' and resp[0].response.get('status') == 'SUCCESS'):
                    is_successful = True
            
            # 如果成功，或者Agent循环正常结束（达到max_iterations），都应跳出重试循环
            break

        except ContextWindowExceededError:
            print(f"\n--- 警告: 第 {attempt + 1}/{MAX_RETRIES} 次尝试因上下文窗口超限失败。---")
            if attempt + 1 >= MAX_RETRIES:
                print(f"--- 错误: 已达到最大重试次数。放弃处理项目 {project_name}。---")
                return False, None
            
            print("--- 正在尝试截断 prompt 文件并重试... ---")
            truncate_prompt_file("generated_prompt_file/prompt.txt")
            await asyncio.sleep(5)
            # continue 会自动进入下一次 for 循环

        except Exception as e:
            print(f"--- 严重错误: 处理项目 {project_name} 时发生未捕获的异常: {e} ---")
            import traceback
            traceback.print_exc()
            # 发生未知严重错误，直接返回失败，不再重试
            return False, None
            
    # --- 循环结束后，解析最终结果并返回 ---
    project_config_path = None
    if is_successful and final_basic_information:
        info_str = ""
        if isinstance(final_basic_information, str):
            info_str = final_basic_information
        elif isinstance(final_basic_information, dict):
            project_config_path = final_basic_information.get('project_config_path')

        if not project_config_path and info_str:
            try:
                # 1. 尝试用正则表达式查找 ```json ... ``` 代码块
                match = re.search(r"```json\s*([\s\S]*?)\s*```", info_str, re.MULTILINE)
                if match:
                    json_str = match.group(1)
                    info_dict = json.loads(json_str)
                    project_config_path = info_dict.get('project_config_path')
                else:
                    # 2. 如果没有代码块，尝试直接加载整个字符串
                    try:
                        info_dict = json.loads(info_str)
                        project_config_path = info_dict.get('project_config_path')
                    except json.JSONDecodeError:
                        # 3. 如果直接加载失败，尝试提取第一个 '{' 和最后一个 '}' 之间的内容
                        start = info_str.find('{')
                        end = info_str.rfind('}')
                        if start != -1 and end != -1 and start < end:
                            json_str = info_str[start:end+1]
                            info_dict = json.loads(json_str)
                            project_config_path = info_dict.get('project_config_path')
            except (json.JSONDecodeError, AttributeError, IndexError) as e:
                 print(f"--- 警告：解析 'basic_information' 失败，无法获取配置路径。错误: {e} ---")

    return is_successful, project_config_path

# ==============================================================================
# 主执行逻辑
# ==============================================================================
async def main():
    print("--- 启动自动化修复工作流 ---")
    GLOBAL_LOGGER.init()
    EXCEL_FILE = 'reproduce_report.xlsx'
    session_service = InMemorySessionService() # 在外部创建一次

    projects_result = read_projects_from_excel(EXCEL_FILE)
    if projects_result['status'] == 'error':
        print(f"错误: 无法处理 Excel 文件: {projects_result['message']}")
        return
    projects_to_process = projects_result.get('projects', [])
    if not projects_to_process:
        print("--- 在 Excel 文件中没有找到需要处理的新项目。工作流结束。 ---")
        return
    print(f"--- 发现 {len(projects_to_process)} 个待处理项目 ---")

    for project_info in projects_to_process:
        project_name = project_info['project_name']
        row_index = project_info['row_index']

        print(f"\n{'='*60}")
        print(f"--- 开始处理项目: {project_name} (行号: {row_index}) ---")
        print(f"{'='*60}")
        
        # 调用独立的、带重试逻辑的函数
        is_successful, project_config_path = await process_single_project(project_info, session_service)
        
        # 如果成功，执行归档
        if is_successful and project_config_path:
            print(f"--- 项目成功修复，正在归档配置文件从: {project_config_path} ---")
            archive_result = archive_fixed_project(project_name, project_config_path)
            if archive_result['status'] == 'error':
                print(f"--- 严重警告: 归档失败! 错误: {archive_result['message']} ---")
        elif is_successful and not project_config_path:
             print("--- 警告：项目修复成功，但无法获取项目配置路径，跳过归档。 ---")
        
        # 回写 Excel
        result_str = "成功" if is_successful else "失败"
        print(f"--- 项目 {project_name} 处理完成，结果: {result_str} ---")
        update_result = update_excel_report(EXCEL_FILE, row_index, "是", result_str)
        if update_result['status'] == 'error':
            print(f"--- 严重警告: 无法将结果写回 Excel 文件! 错误: {update_result['message']} ---")

        # 清理环境
        print("--- 正在清理环境... ---")
        if os.path.exists("fuzz_build_log_file"): shutil.rmtree("fuzz_build_log_file")
        if os.path.exists("generated_prompt_file"): shutil.rmtree("generated_prompt_file")
        if os.path.exists("solution.txt"): os.remove("solution.txt")
        safe_project_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        project_source_path = os.path.join(os.getcwd(), "process", "project", safe_project_name)
        if os.path.exists(project_source_path): shutil.rmtree(project_source_path)

        print(f"--- 已清理临时文件和项目源码，准备处理下一个项目 ---")
        await asyncio.sleep(5)

    print("\n--- 所有项目处理完毕。工作流正常结束。 ---")

if __name__ == "__main__":
    # (启动检查部分保持不变)
    print("--- 正在进行启动前检查... ---")
    if not DPSEEK_API_KEY:
        print("\n[错误] 启动失败: DPSEEK_API_KEY 未设置。")
        print("请执行以下操作之一:")
        print("  - 创建一个名为 '.env' 的文件，并在其中写入: DPSEEK_API_KEY='your_api_key_here'")
        print("  - 或者，在运行脚本前执行: export DPSEEK_API_KEY='your_api_key_here'")
    else:
        print("✅ DPSEEK_API_KEY 已设置。")
        try:
            subprocess.run(["gh", "--version"], check=True, capture_output=True, text=True)
            print("✅ GitHub CLI ('gh') 已安装。")
            subprocess.run(["gh", "auth", "status"], check=True, capture_output=True)
            print("✅ GitHub CLI ('gh') 已登录。")
            print("\n--- 检查完毕，准备启动 Agent... ---")
            asyncio.run(main())
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print("\n[错误] 启动失败: GitHub CLI ('gh') 未安装或未登录。")
            print("请先安装 gh-cli 并运行 'gh auth login' 进行认证。")
            print(f"错误详情: {e}")
