import os
import shutil
import time
import json
import re
import sys
import traceback
import asyncio
import subprocess
import litellm
import logging
from datetime import datetime, timedelta
from typing import Dict, AsyncGenerator, Tuple, Optional
from dotenv import load_dotenv
from agent_tools import ENABLE_HISTORY_ENHANCEMENT, ENABLE_REFLECTION, ENABLE_ROLLBACK, ENABLE_EXPERT_KNOWLEDGE

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
    archive_fixed_project,
    download_remote_log,
    download_github_repo,
    force_clean_git_repo,
    checkout_oss_fuzz_commit,
    extract_build_metadata_from_log,
    patch_project_dockerfile,
    get_project_paths,
    checkout_project_commit,
    read_file_content,
    get_git_commits_around_date,
    save_commit_diff_to_file,
    create_or_update_file,
    run_command,
    extract_buggy_line_info,
    get_enhanced_history_context,
    run_fuzz_build_and_validate,
    apply_patch,
    update_reflection_journal,
    manage_git_state,
    clear_commit_analysis_state,
    prompt_generate_tool,
    query_expert_knowledge,
    append_string_to_file,
    find_and_append_file_details,
    prune_session_history,
    save_file_tree_shallow
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
    def __init__(self, log_directory: str = "agent_logs"):
        """使用 __init__ 确保在对象创建时，所有基础属性都已存在，防止重定向后的 print 报错"""
        self.log_directory = log_directory
        self.logger = None
        self.file_handler_setup = False
        self.log_buffer = []
        self.project_name = "orchestrator"
        os.makedirs(self.log_directory, exist_ok=True)

    def set_project_context(self, project_name: str):
        if self.logger:
            for handler in self.logger.handlers[:]:
                handler.close()
                self.logger.removeHandler(handler)
        self.project_name = project_name
        self.file_handler_setup = False
        self.setup_file_handler()

    def setup_file_handler(self):
        if self.file_handler_setup: return
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

        # 注意：此处 print 会被 StreamTee 捕获，但由于 file_handler_setup 尚未置为 True，
        # 它会先进入 log_buffer，保证顺序正确
        print(f"✅ Log file created: {log_filepath}")

        for log_entry in self.log_buffer:
            self.logger.info(log_entry)
        self.log_buffer = []
        self.file_handler_setup = True

    def log_raw(self, message: str):
        msg = message.rstrip()
        if not msg: return
        if self.file_handler_setup and self.logger:
            self.logger.info(msg)
        else:
            self.log_buffer.append(msg)

    def log_event(self, event: Event):
        log_message = self._format_message(event)
        if log_message:
            print(log_message)

    def _format_message(self, event: Event) -> str:
        author = event.author
        log_parts = [f"EVENT from author: '{author}'"]
        if event.usage_metadata:
            u = event.usage_metadata
            log_parts.append(f"  - TOKEN_USAGE: Prompt={u.prompt_token_count}, Gen={u.candidates_token_count}")
        if hasattr(event, 'get_function_calls') and (func_calls := event.get_function_calls()):
            for call in func_calls: log_parts.append(
                f"  - TOOL_CALL: {call.name}({json.dumps(call.args, ensure_ascii=False)})")
        if hasattr(event, 'get_function_responses') and (func_resps := event.get_function_responses()):
            for resp in func_resps:
                response_str = str(resp.response)
                response_str = response_str[:500] + "..." if len(response_str) > 500 else response_str
                log_parts.append(f"  - TOOL_RESPONSE for '{resp.name}': {response_str}")
        if (actions := event.actions):
            if actions.state_delta: log_parts.append(f"  - STATE_UPDATE: {actions.state_delta}")
            if actions.escalate: log_parts.append("  - ACTION: Escalate (Agent Finish)")
        return "\n".join(log_parts)


# ==========================================
# 新增：双向流管家 (Tee)
# ==========================================
class StreamTee:
    """同时将输出发送到原始流（屏幕）和 Logger（文件）"""

    def __init__(self, original_stream, agent_logger):
        self.original_stream = original_stream
        self.agent_logger = agent_logger

    def write(self, data):
        self.original_stream.write(data)
        # 实时写入日志文件
        if data.strip():
            self.agent_logger.log_raw(data)

    def flush(self):
        self.original_stream.flush()

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
MODEL = "deepseek/deepseek-chat"
DPSEEK_API_KEY = os.getenv("DPSEEK_API_KEY")
USER_ID = "default_user"
MAX_RETRIES = 3
LLM_SEED = 42
top_p= 0.9

# --- Environment Preparation Agent ---
initial_setup_agent = LlmAgent(
    name="initial_setup_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.5, top_p=top_p, seed=LLM_SEED),
    instruction=load_instruction_from_file("instructions/initial_setup_instruction.txt"),
    tools=[
        download_github_repo,
        force_clean_git_repo,
        checkout_oss_fuzz_commit,
        extract_build_metadata_from_log,
        patch_project_dockerfile,
        get_project_paths,
        manage_git_state,
        checkout_project_commit,
    ],
    output_key="basic_information",
)


# --- Agents in the Fix Loop ---
def exit_loop(tool_context: ToolContext):
    tool_context.actions.escalate = True
    return {"status": "SUCCESS"}

run_fuzz_and_collect_log_agent = LlmAgent(
    name="run_fuzz_and_collect_log_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.6, top_p=top_p, seed=LLM_SEED),
    instruction=load_instruction_from_file("instructions/run_fuzz_and_collect_log_instruction.txt"),
    tools=[read_file_content, run_command, run_fuzz_build_and_validate, create_or_update_file],
    output_key="fuzz_build_log",
)

decision_agent = LlmAgent(
    name="decision_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.3, top_p=top_p, seed=LLM_SEED),
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
        get_project_paths,
        extract_buggy_line_info,
        get_enhanced_history_context,
    ],
    output_key="commit_analysis_result",
)

reflection_agent = LlmAgent(
    name="reflection_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=load_instruction_from_file("instructions/reflection_instruction.txt"),
    tools=[read_file_content, update_reflection_journal],
    output_key="last_reflection_result"
)

rollback_agent = LlmAgent(
    name="rollback_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.6, top_p=top_p, seed=LLM_SEED),
    instruction=load_instruction_from_file("instructions/rollback_instruction.txt"),
    tools=[manage_git_state, clear_commit_analysis_state],
)

prompt_generate_agent = LlmAgent(
    name="prompt_generate_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=16384, temperature=0.6, top_p=top_p, seed=LLM_SEED),
    instruction=load_instruction_from_file("instructions/prompt_generate_instruction.txt"),
    tools=[
        prompt_generate_tool, 
        run_command,  
        save_file_tree_shallow, 
        find_and_append_file_details, 
        read_file_content, 
        create_or_update_file, 
        append_string_to_file,
        query_expert_knowledge, 
    ],
    output_key="generated_prompt",
)

fuzzing_solver_agent = LlmAgent(
    name="fuzzing_solver_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=8129, temperature=0.6, top_p=top_p, seed=LLM_SEED),
    instruction=load_instruction_from_file("instructions/fuzzing_solver_instruction.txt"),
    tools=[read_file_content, create_or_update_file],
    output_key="solution_plan",
)

solution_applier_agent = LlmAgent(
    name="solution_applier_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.6, top_p=top_p, seed=LLM_SEED),
    instruction=load_instruction_from_file("instructions/solution_applier_instruction.txt"),
    tools=[apply_patch, read_file_content, manage_git_state],
    output_key="patch_application_result",
)

summary_agent = LlmAgent(
    name="summary_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.6, top_p=top_p, seed=LLM_SEED),
    instruction=load_instruction_from_file("instructions/summary_instruction.txt"),
    tools=[prune_session_history],
    output_key=".", 
)

# --- Workflow Definition ---
loop_sub_agents = [
    run_fuzz_and_collect_log_agent,
    decision_agent,
]

if ENABLE_REFLECTION:
    loop_sub_agents.append(reflection_agent)

if ENABLE_ROLLBACK:
    loop_sub_agents.append(rollback_agent)

loop_sub_agents.extend([
    commit_finder_agent,
    prompt_generate_agent,
    fuzzing_solver_agent,
    solution_applier_agent,
    summary_agent,
])

workflow_loop_agent = LoopAgent(
    name="workflow_loop_agent",
    sub_agents=loop_sub_agents,
    max_iterations=15
)

subject_agent = SequentialAgent(
    name="fix_fuzz_agent",
    sub_agents=[initial_setup_agent, workflow_loop_agent],
    description="A workflow that automatically downloads, configures, and iteratively fixes Fuzzing build issues"
)

root_agent = LoggingWrapperAgent(subject_agent=subject_agent)


def cleanup_environment(project_name: str):
    """
    【焦土清理版】
    除了源代码，必须物理抹除所有中间状态、历史分析结果、反思日志，
    彻底杜绝跨项目污染。
    """
    import shutil
    import os
    print(f"--- 🧹 Tool: cleanup_environment for: {project_name} ---")

    # 必须清理的物理路径
    paths_to_remove = [
        "fuzz_build_log_file",       # 编译日志
        "generated_prompt_file",     # 包含 commit_changed.txt (关键！)
        "solution.txt",              # 上一次生成的 patch
        "file_tree.txt",             # 文件树缓存
        "reflection_journal.json"    # 反思日志 (关键！)
    ]

    for path in paths_to_remove:
        if os.path.exists(path):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                print(f"  - Cleaned: {path}")
            except Exception as e:
                print(f"  - Warning: Failed to clean {path}: {e}")


def save_full_fixed_content(project_name: str, config_path: str, source_path: str):
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_base_dir = os.path.join(os.getcwd(), "process", "fixed", f"{project_name}-{timestamp}")
    
    os.makedirs(target_base_dir, exist_ok=True)
    print(f"--- 💾 Archiving successful fix to: {target_base_dir} ---")
    
    repos = [("config", config_path), ("source", source_path)]
    for label, repo_path in repos:
        if not repo_path or not os.path.exists(repo_path): continue
        try:
            # 这里的 git show 逻辑保持不变
            result = subprocess.run(["git", "-C", repo_path, "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True, check=True)
            files = [f.strip() for f in result.stdout.split('\n') if f.strip()]
            for f_rel in files:
                abs_src = os.path.join(repo_path, f_rel)
                if os.path.exists(abs_src) and os.path.isfile(abs_src):
                    abs_dest = os.path.join(target_base_dir, f_rel)
                    os.makedirs(os.path.dirname(abs_dest), exist_ok=True)
                    shutil.copy2(abs_src, abs_dest)
                    print(f"  - [{label}] Saved: {f_rel}")
        except Exception as e:
            print(f"  - [{label}] Skip saving: {e}")


async def process_single_project(
    project_info: Dict,
    session_service: InMemorySessionService
) -> Tuple[bool, Optional[str]]:
    """
    【彻底重置与双级追踪版 - 极致订正】
    1. 物理重置：大循环切换时物理删除 reflection_journal.json。
    2. 记忆延续：仅通过 Python 内存保留 full_deterioration_history。
    3. 成本追踪：区分展示“本轮消耗”与“项目总消耗”。
    4. 路径防御：剥离 LLM 响应中的自然语言，提取纯净配置路径。
    """
    project_name = project_info['project_name']
    oss_fuzz_sha = project_info['sha']
    software_sha = project_info.get('software_sha', "N/A")
    original_log_path = project_info.get('original_log_path', "")
    
    # --- 1. 初始化项目级持久化指标 (跨 Attempt 不重置) ---
    project_start_time = time.time()
    project_total_tokens = {"prompt": 0, "completion": 0, "total": 0}
    full_deterioration_history = []
    
    # 动态超时：支持内层20轮深度修复，设为4小时
    TIMEOUT_LIMIT = 14400  
    is_successful = False
    final_basic_information = None
    last_run_stats = {}

    for attempt in range(MAX_RETRIES):
        # 【关键步骤 A】物理清空环境（删除反思日志、根因报告等，防止 A1 污染 A2）
        cleanup_environment(project_name)

        # 设置当前大循环 ID (1-based)
        current_attempt_id = attempt + 1

        # 【关键步骤 B】手动重置本轮 Attempt 的独立计数器
        stats = {
            "repair_rounds": 0,       # 本轮实际修复次数
            "build_calls": 0,         # 本轮构建总调用次数
            "rollback_count": 0,      # 本轮物理回退次数
            "total_tokens": {"prompt": 0, "completion": 0, "total": 0}, # 仅本轮消耗
            "code_gen_tokens": 0,     
            "scores": full_deterioration_history, # 引用外部列表，确保轨迹跨 Attempt 连续展示
            "decision_type": "UNKNOWN", 
            "patch_impact": {"files": 0, "lines": 0}, 
            "heuristic_used": False,
            "attempt_id": current_attempt_id
        }
        last_run_stats = stats

        # 创建 Session 并注入 attempt_id 到状态机
        current_session_id = f"session_{project_name}_{int(time.time())}_at{attempt}"
        await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=current_session_id)
        session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=current_session_id)
        session.state["attempt_id"] = current_attempt_id # 确保 Agent 能通过上下文读取到 ID

        GLOBAL_LOGGER.set_project_context(project_name)
        runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

        # 构造输入消息
        safe_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        expected_source_path = os.path.join(os.getcwd(), "process", "project", safe_name)
        initial_input = json.dumps({
            "project_name": project_name,
            "oss_fuzz_sha": oss_fuzz_sha,
            "original_log_path": original_log_path,
            "project_source_path": expected_source_path,
            "software_repo_url": project_info.get('software_repo_url', ""),
            "software_sha": software_sha,
            "engine": project_info.get('engine', ""),
            "sanitizer": project_info.get('sanitizer', ""),
            "architecture": project_info.get('architecture', ""),
            "base_image_digest": project_info.get('base_image_digest', ""),
            "attempt_id": current_attempt_id # 显式传入
        })
        initial_message = types.Content(parts=[types.Part(text=initial_input)], role='user')

        try:
            print(f"\n--- 🌀 Starting Attempt {current_attempt_id}/{MAX_RETRIES} (Clean State) ---")
            
            async for event in runner.run_async(user_id=USER_ID, session_id=current_session_id, new_message=initial_message):
                # A. Token 统计与累加
                if event.usage_metadata:
                    p = getattr(event.usage_metadata, "prompt_token_count", 0) or 0
                    c = getattr(event.usage_metadata, "candidates_token_count", 0) or 0
                    stats["total_tokens"]["prompt"] += p
                    stats["total_tokens"]["completion"] += c
                    stats["total_tokens"]["total"] += (p + c)
                    project_total_tokens["total"] += (p + c) # 即使崩溃，Token 消耗也记录在案
                    if event.author == 'fuzzing_solver_agent': stats["code_gen_tokens"] += c

                # B. 启发式监控
                if event.author == 'commit_finder_agent' and (func_calls := event.get_function_calls()):
                    if any(fc.name in ['extract_buggy_line_info', 'get_enhanced_history_context'] for fc in func_calls):
                        stats["heuristic_used"] = True

                # C. 决策类型解析
                if event.author == 'fuzzing_solver_agent' and event.content:
                    parts = [p.text for p in event.content.parts if hasattr(p, 'text') and p.text]
                    full_text = "".join(parts)
                    match = re.search(r"\[(RULE-DRIVEN|AUTONOMOUS|HYBRID)\]", full_text)
                    if match: stats["decision_type"] = match.group(1)

                # D. 修复轮数统计 (仅计本轮)
                if event.author == 'run_fuzz_and_collect_log_agent' and (f_calls := event.get_function_calls()):
                    if any(c.name == 'run_fuzz_build_streaming' for c in f_calls):
                        stats["build_calls"] += 1
                        stats["repair_rounds"] = max(0, stats["build_calls"] - 1)

                # E. 评分与轨迹持久化 (修正版：彻底修复 AttributeError 并保持双级追踪)
                if (resps := event.get_function_responses()):
                    for r in resps:
                        # 1. 处理反思日志评分
                        if r.name == 'update_reflection_journal':
                            s = r.response.get('deterioration_score', 0)
                            # 使用当前 Attempt 内的构建次数作为内循环 Round ID
                            inner_round = stats["build_calls"]
                            full_deterioration_history.append(f"A{current_attempt_id}_R{inner_round}:{s}")

                        # 2. 处理回退统计 (修复点：通过 response 消息内容判定，不访问不存在的 .args)
                        if r.name == 'manage_git_state' and r.response.get('status') == 'success':
                            # 检查返回消息中是否包含 "Rolled back" 关键字
                            msg = str(r.response.get('message', ''))
                            if "Rolled back" in msg or "rollback" in msg.lower():
                                stats["rollback_count"] += 1

                        # 3. 处理补丁规模统计
                        if r.name == 'apply_patch' and r.response.get('status') in ['success', 'partial_success']:
                            stats["patch_impact"]["files"] = r.response.get('modified_files_count', 0)
                            stats["patch_impact"]["lines"] = r.response.get('total_lines_changed', 0)

                if (func_resps := event.get_function_responses()):
                    for resp in func_resps:
                        if resp.name == 'run_fuzz_build_and_validate':
                            val_report = resp.response.get('validation_report')
                            if val_report:
                                # 物理更新当前 session 的 state
                                session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID,
                                                                            session_id=current_session_id)
                                session.state["last_validation_report"] = val_report
                                # 打印物理简报以便在控制台监控
                                print(
                                    f"\n[1+6 Audit] Step 1: {val_report.get('step_1_static_output')} | Step 6: {val_report.get('step_6_runtime_stability')}")

                if event.author == 'initial_setup_agent' and event.actions and event.actions.state_delta:
                    if 'basic_information' in event.actions.state_delta:
                        full_info = event.actions.state_delta['basic_information']
                        try:
                            # 物理保存包含 dependencies 的全量数据到项目目录
                            meta_save_path = os.path.join(expected_source_path, "metadata.json")
                            os.makedirs(os.path.dirname(meta_save_path), exist_ok=True)

                            data_to_write = None
                            if isinstance(full_info, str):
                                json_match = re.search(r'(\{[\s\S]*\})', full_info)
                                if json_match:
                                    clean_json = json_match.group(1)
                                    data_to_write = json.loads(clean_json)
                                else:
                                    raise ValueError("No JSON structure found in response text")
                            else:
                                data_to_write = full_info

                            if data_to_write:
                                with open(meta_save_path, "w", encoding='utf-8') as mf:
                                    json.dump(data_to_write, mf, indent=2, ensure_ascii=False)
                                print(f"--- 💾 Full metadata (including dependencies) archived to: {meta_save_path} ---")
                        except Exception as meta_e:
                            # 仅打印警告，不中断主流程
                            print(f"--- ⚠️ Metadata archive failed: {meta_e} ---")

                # F. 成功判定
                if (event.actions and event.actions.escalate and
                    event.author == 'decision_agent' and
                    (resp := event.get_function_responses()) and
                    resp[0].name == 'exit_loop' and resp[0].response.get('status') == 'SUCCESS'):
                    is_successful = True
                    final_basic_information = session.state.get('basic_information')

            # --- 轮次结束逻辑 ---
            if (time.time() - project_start_time) > TIMEOUT_LIMIT:
                print(f"--- ❌ [TIMEOUT] Project {project_name} reached limit. ---")
                break

            if is_successful:
                break
            else:
                # 核心设计：如果模型逻辑跑完没修好，不再进行下一次 Attempt 重试（API/异常除外）
                print(f"--- ⚠️ [GIVE UP] Project {project_name} failed internal logic. ---")
                break

        except litellm.ContextWindowExceededError as e:
            # 专门记录 Token 溢出异常
            error_msg = f"--- 🚨 [CONTEXT LIMIT] Attempt {current_attempt_id} hit 13.1w limit: {str(e)} ---"
            print(error_msg)
            if GLOBAL_LOGGER.logger:
                GLOBAL_LOGGER.logger.error(f"{error_msg}\n{traceback.format_exc()}")
            if attempt + 1 >= MAX_RETRIES: break
            continue


        except Exception as e:
            # 捕获所有其他物理崩溃（网络、API、代码 Bug）
            error_stack = traceback.format_exc()
            error_msg = f"--- ❌ [CRASH] Attempt {current_attempt_id} failed with {type(e).__name__}: {str(e)} ---"
            print(error_msg)

            # 【关键补丁】：物理记录堆栈到日志文件，防止日志“断片”
            if GLOBAL_LOGGER.logger:
                GLOBAL_LOGGER.logger.error(f"{error_msg}\n--- DETAILED STACK ---\n{error_stack}")

            if attempt + 1 >= MAX_RETRIES: break
            # 策略：遇到未知崩溃，利用大循环的 cleanup_environment 进行物理环境重置并重试
            continue

    # --- 3. 最终项目总结报告 (显示最后一次有效 Attempt 的数据) ---
    final_duration_min = (time.time() - project_start_time) / 60
    l_stats = last_run_stats if last_run_stats else stats # 防御性引用

    summary_report = (
        f"\n{'='*60}\n"
        f"🏁 FINAL PROJECT REPAIR REPORT: {project_name}\n"
        f"{'-'*60}\n"
        f"  - [RESULT]           {'✅ SUCCESS' if is_successful else '❌ FAILURE'}\n"
        f"  - [TARGET SHA]       {software_sha}\n"
        f"  - [REPAIR ROUNDS]     {l_stats.get('repair_rounds', 0)} (In Last Attempt)\n"
        f"  - [ROLLBACKS]         {l_stats.get('rollback_count', 0)} (In Last Attempt)\n"
        f"  - [DETERIORATION]     {' -> '.join(full_deterioration_history) if full_deterioration_history else 'N/A'}\n"
        f"  - [LAST ATTEMPT TOKENS]\n"
        f"      Input (Prompt):   {l_stats['total_tokens']['prompt']}\n"
        f"      Output (Gen):     {l_stats['total_tokens']['completion']}\n"
        f"      Code Gen Only:    {l_stats.get('code_gen_tokens', 0)}\n"
        f"  - [PROJECT TOTAL COST]\n"
        f"      Total Tokens:     {project_total_tokens['total']}\n"
        f"      Total Time Cost:  {final_duration_min:.2f} minutes\n"
        f"  - [PATCH SCALE]       {l_stats['patch_impact']['files']} files, {l_stats['patch_impact']['lines']} lines\n"
        f"{'='*60}\n"
    )

    print(summary_report)
    if GLOBAL_LOGGER.logger: GLOBAL_LOGGER.logger.info(summary_report)

    if is_successful:
        try:
            with open("fix-success.txt", "a") as f: f.write(f"{project_name}\n")
        except: pass

    # --- 4. 解析配置路径 (修正版：剥离自然语言废话，提取纯路径) ---
    clean_config_path = None
    if is_successful and final_basic_information:
        text_content = str(final_basic_information)
        try:
            # 策略 1: 寻找 Markdown JSON 块
            json_block = re.search(r"```json\s*([\s\S]*?)\s*```", text_content)
            if json_block:
                data = json.loads(json_block.group(1))
                clean_config_path = data.get('project_config_path')
            else:
                # 策略 2: 直接正则提取 oss-fuzz 物理路径模式 (解决 hiredis 等项目废话太多的问题)
                # 匹配形如 /.../oss-fuzz/projects/hiredis 的字符串
                path_match = re.search(r"(/[^ ]+/oss-fuzz/projects/[^/ \"`]+)", text_content)
                if path_match:
                    clean_config_path = path_match.group(1).strip().rstrip('\"').rstrip('`').rstrip('.')
        except: pass

    # 路径安全校验：如果剥离出的路径还有非法字符，置为 None 防止下游归档崩溃
    if clean_config_path and ("\n" in clean_config_path or " " in clean_config_path):
        clean_config_path = None

    # 【最终返回点】：确保返回剥离干净后的纯路径
    return is_successful, clean_config_path



async def main():
    """
    【大师级主循环】
    1. 实现了项目间的物理隔离。
    2. 确保环境清理不损害已下载的源代码。
    3. 完善的报告更新与归档机制。
    """
    print("--- Starting automated fix workflow ---")

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
        initial_input_data = {
            "project_name": project_name,
            "sha": project_info['sha'],
            "original_log_path": project_info['original_log_path'],
            "software_repo_url": project_info['software_repo_url'],
            "software_sha": project_info['software_sha'],
            "engine": project_info['engine'],
            "sanitizer": project_info['sanitizer'],
            "architecture": project_info['architecture'],
            "base_image_digest": project_info['base_image_digest']
        }

        print(f"\n{'='*60}")
        print(f"--- Processing Project: {project_name} (Index: {row_index}) ---")
        print(f"{'='*60}")

        update_yaml_report(YAML_FILE, row_index, "Failure (Crashed/In_Progress)")
        cleanup_environment(project_name)

        # 执行修复流程
        is_successful, project_config_path = await process_single_project(initial_input_data, session_service)


# --- 处理修复成功后的逻辑 (最终订正版) ---
        if is_successful:
            print(f"\n{'='*20} SUCCESS PROCESSING {'='*20}")
            print(f"--- [SUCCESS] Project {project_name} fixed. ---")

            # 1. 确定源码物理路径（逻辑与 process_single_project 内部完全对齐）
            safe_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
            project_source_path = os.path.join(os.getcwd(), "process", "project", safe_name)

            # 2. 【执行保存策略】：保存改动文件的完整内容
            # 存储路径：process/fixed/<project_name>-<timestamp>/
            # 该函数内部已包含对 config_path 和 source_path 的 Git 变更提取逻辑
            try:
                save_full_fixed_content(project_name, project_config_path, project_source_path)
            except Exception as e:
                print(f"--- [ERROR] Failed to save fixed full content: {e} ---")

            # 3. 归档 OSS-Fuzz 配置 (Dockerfile, build.sh)
            if project_config_path:
                print(f"--- Archiving config from: {project_config_path} ---")
                # 【修正】：将返回值赋予 archive_result，避免后续检查报 NameError
                archive_result = archive_fixed_project(project_name, project_config_path)

                if archive_result.get('status') == 'error':
                    print(f"--- [CRITICAL] Archiving failed: {archive_result.get('message')} ---")
                else:
                    print(f"--- [SUCCESS] Config archive completed. ---")
            else:
                print(f"--- [WARNING] Project fixed, but config path missing. Skipping archive. ---")
            
            print(f"{'='*60}\n")
        
        elif not is_successful:
             print(f"--- [FAILURE] Project {project_name} could not be fixed within allowed attempts. ---")

        # 更新 YAML 状态报告
        result_str = "Success" if is_successful else "Failure"
        print(f"--- Project {project_name} complete. Result: {result_str} ---")
        update_result = update_yaml_report(YAML_FILE, row_index, result_str)

        if update_result['status'] == 'error':
            print(f"--- [CRITICAL] Could not update YAML report: {update_result['message']} ---")

        # 【关键步骤 2】: 项目结束后清理
        
        cleanup_environment(project_name)


    print("\n--- All projects in the queue have been processed. Workflow finished. ---")



if __name__ == "__main__":
    print("--- Performing pre-startup checks... ---")
    sys.stdout = StreamTee(sys.stdout, GLOBAL_LOGGER)
    sys.stderr = StreamTee(sys.stderr, GLOBAL_LOGGER)
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
            # --- START MODIFICATION ---
            # 检查 requests 库是否安装
            try:
                import requests
                print("✅ 'requests' library is installed.")
            except ImportError:
                print("\n[ERROR] Startup failed: 'requests' library is not installed.")
                print("Please install it by running: pip install requests")
                sys.exit(1) # 退出程序
            # --- END MODIFICATION ---
            subprocess.run(["gh", "auth", "status"], check=True, capture_output=True)
            print("✅ GitHub CLI ('gh') is logged in.")
            print("\n--- Checks complete. Preparing to start the Agent... ---")
            asyncio.run(main())
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print("\n[ERROR] Startup failed: GitHub CLI ('gh') is not installed or not logged in.")
            print("Please install the gh-cli first and authenticate by running 'gh auth login'.")
            print(f"Error details: {e}")
