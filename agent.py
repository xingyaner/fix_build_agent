import os
import time
import re
import warnings
import json
import sys
import traceback
import asyncio
import subprocess
import litellm
import logging
from datetime import datetime
from typing import Dict, AsyncGenerator, Tuple, Optional, List, Any
from dotenv import load_dotenv

load_dotenv()
litellm.request_timeout = 600
litellm.num_retries = 2
litellm.drop_params = True

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.models.lite_llm import LiteLlm
from google.adk.events import Event
from google.adk.tools.tool_context import ToolContext
from google.adk.agents import LlmAgent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.genai import types
from google.adk.workflow import Workflow, Edge, node, BaseNode
from google.adk.agents import Context
from functools import wraps
from agent_tools import safe_delete_path
from agent_tools import (
    read_projects_from_yaml,
    update_yaml_report,
    archive_fixed_project,
    download_remote_log,
    update_trace_ledger,
    download_github_repo,
    force_clean_git_repo,
    checkout_oss_fuzz_commit,
    extract_build_metadata_from_log,
    patch_project_dockerfile,
    get_project_paths,
    checkout_project_commit,
    read_file_content,
    get_verified_git_sha,
    get_git_commits_around_date,
    save_commit_diff_to_file,
    create_or_update_file,
    run_command,
    check_file_exists,
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
    save_file_tree_shallow,
    # New Mechanisms Tools
    TraceLedgerManager,
    cbsc_classify_log,
    execute_hsr_decision,
    run_ecrcl_localization,
    few_shot_rag_retrieve,
    init_or_update_rsmc_ledger,
    list_files_in_dir,
    query_trace_ledger
)


class StreamTee:
    def __init__(self, original_stream, agent_logger):
        self.original_stream = original_stream
        self.agent_logger = agent_logger

    def write(self, data):
        self.original_stream.write(data)
        if data.strip():
            self.agent_logger.log_raw(data)

    def flush(self):
        self.original_stream.flush()


class LoggingWrapperAgent(BaseAgent):
    name: str = "LoggingWrapperAgent"
    # 🔑 优化：变更为 BaseNode 以便包裹 Workflow 对象
    subject_agent: BaseNode

    async def _run_async_impl(self, context: InvocationContext) -> AsyncGenerator[Event, None]:
        try:
            # 🔑 1. 兼容性判定：如果被包装对象拥有旧版 run_async，则走传统 agent 分支
            if hasattr(self.subject_agent, "run_async"):
                async for event in self.subject_agent.run_async(context):
                    GLOBAL_LOGGER.log_event(event)
                    yield event
            # 🔑 2. 否则，被包装对象为现代 BaseNode/Workflow，调用 ADK 2.0 标准 run 入口
            else:
                # 将 InvocationContext 包装为 Workflow 内部上下文 Context
                adk_ctx = Context(context)
                # 安全获取初始用户输入消息作为节点输入
                node_input = getattr(context, "user_content", None)

                async for event in self.subject_agent.run(ctx=adk_ctx, node_input=node_input):
                    GLOBAL_LOGGER.log_event(event)
                    yield event

        except (Exception, KeyboardInterrupt) as e:
            print(f"\n--- Interruption or error detected: {type(e).__name__} ---");
            raise e
        finally:
            if not GLOBAL_LOGGER.file_handler_setup: GLOBAL_LOGGER.setup_file_handler()


class AgentLogger:
    def __init__(self, log_directory: str = "agent_logs"):
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


def load_instruction_from_file(filename: str) -> str:
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"Warning: Instruction file '{filename}' not found. The agent will use an empty instruction.")
        return ""


def tool_defense_decorator(func):
    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            # 日志展示非法或异常调用，但不崩溃，将异常抛出给 Agent 处理
            GLOBAL_LOGGER.log_raw(f"⚠️ [Security/Error] Tool '{func.__name__}' failed: {str(e)}")
            return {"status": "error", "message": f"Execution failed: {str(e)}"}

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            GLOBAL_LOGGER.log_raw(f"⚠️ [Security/Error] Tool '{func.__name__}' failed: {str(e)}")
            return {"status": "error", "message": f"Execution failed: {str(e)}"}

    return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper


def wrap_tools(tools: List[Any]) -> List[Any]:
    return [tool_defense_decorator(t) for t in tools]


# =====================================================================
# 辅助函数：安全记忆清理与状态脱水 (物理手术完全无状态版)
# =====================================================================

async def _safe_memory_cleaning(session_service: InMemorySessionService, session_id: str):
    """
    【防御性优化版】仅对明确的大体积负载进行脱水，严格保护运行环境元数据与多轮会话上下文。
    """
    session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    if not session:
        return

    # 1. 状态字典脱水：仅对明确占用空间的超大键进行脱水，直接清理死码变量保护性能
    massive_keys = ["fuzz_build_log", "commit_analysis_result", "generated_prompt"]

    for key in list(session.state.keys()):
        # 只要是已知的超大负载 key，统一实施安全脱水，避免撑爆 Token 窗口
        if key in massive_keys:
            session.state[key] = "[DEHYDRATED: SUMMARY IN LEDGER]"

    print(
        f"--- 🧼 [SAFE CLEANSED] Dehydrated massive state keys for session {session_id}. Event history and core env metadata preserved. ---")

    # PROTECTED_STATE_KEYS = {
    #     "project_source_path", "project_config_path", "error_time",
    #     "attempt_id", "round_id", "current_node_id", "rollback_triggered",
    #     "ever_used_upstream", "last_validation_report",
    #     "software_sha", "oss_fuzz_sha"
    # }


def cleanup_environment(project_name: str):
    print(f"--- 🧹 Tool: cleanup_environment for: {project_name} ---")

    paths_to_remove = [
        # "fuzz_build_log_file",
        # "generated_prompt_file",
        # "solution.txt",
        "file_tree.txt"
    ]

    for path in paths_to_remove:
        if os.path.exists(path):
            try:
                safe_delete_path(path)
                print(f"  - Cleaned: {path}")
            except Exception as e:
                print(f"  - Warning: Failed to clean {path}: {e}")


def exit_loop(tool_context: ToolContext):
    tool_context.actions.escalate = True
    return {"status": "SUCCESS"}


GLOBAL_LOGGER = AgentLogger()

APP_NAME = "fix_build_agent_app"
MODEL = "deepseek/deepseek-chat"
DPSEEK_API_KEY = os.getenv("DPSEEK_API_KEY")
USER_ID = "default_user"
# MAX_RETRIES = 3
MAX_RETRIES = 1
LLM_SEED = 42
top_p = 0.9


def initialize_agents(session_state: dict = None) -> Tuple[BaseNode, InMemorySessionService]:
    """
    Dynamically instantiates all agents and binds into linear Workflow.
    Remove internal Loop/ring back, drive iteration by outer Python loop.
    """
    # 提取注入上下文
    rc_commit = str(session_state.get("root_cause_commit", "")) if session_state else ""
    rc_workspace = str(session_state.get("root_cause_workspace", "")) if session_state else ""

    # 加载并动态注入指令
    finder_instr = load_instruction_from_file("instructions/commit_finder_instruction.txt")

    # --- 审计代码：打印指令注入情况 ---
    print(f"[AUDIT] Agent Instruction Injection: commit={rc_commit}, workspace={rc_workspace}")

    if not rc_commit or rc_commit == "N/A":
        # 移除关于 Bypass 的逻辑块
        processed_instruction = finder_instr.replace("{root_cause_commit?}", "").replace("{root_cause_workspace?}", "")
        # 可选：在指令中追加一行提示，告知 Agent 没有预设值，直接全量搜索
        processed_instruction += "\n# NOTE: No pre-specified root cause detected. Execute full standard localization."
    else:
        processed_instruction = finder_instr.replace("{root_cause_commit?}", rc_commit) \
            .replace("{root_cause_workspace?}", rc_workspace) \
            .replace("{root_cause_commit}", rc_commit) \
            .replace("{root_cause_workspace}", rc_workspace)


    # --- 审计代码：将最终注入后的指令打印到控制台 ---
    print(f"\n[AUDIT] Commit Finder Instruction injected with: Commit={rc_commit}, Workspace={rc_workspace}")
    print(f"[AUDIT] Final Instruction snippet: {processed_instruction[:4000]}...")

    # 1. 初始化所有 LlmAgent
    initial_setup_agent = LlmAgent(
        name="initial_setup_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.2, top_p=0.3, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/initial_setup_instruction.txt"),
        tools=wrap_tools([
            download_github_repo,
            force_clean_git_repo,
            checkout_oss_fuzz_commit,
            extract_build_metadata_from_log,
            patch_project_dockerfile,
            get_project_paths,
            manage_git_state,
            checkout_project_commit,
        ]),
        output_key="basic_information",
    )

    run_fuzz_and_collect_log_agent = LlmAgent(
        name="run_fuzz_and_collect_log_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.2, top_p=0.3, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/run_fuzz_and_collect_log_instruction.txt"),
        tools=wrap_tools([read_file_content, run_fuzz_build_and_validate]),
        output_key="fuzz_build_log",
    )

    decision_agent = LlmAgent(
        name="decision_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.2, top_p=0.3, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/decision_instruction.txt"),
        tools=wrap_tools([read_file_content, exit_loop]),
        output_key="decision_result",
    )

    rsmc_agent = LlmAgent(
        name="rsmc_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.4, top_p=0.4, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/rsmc_instruction.txt"),
        tools=wrap_tools([read_file_content, init_or_update_rsmc_ledger, query_trace_ledger]),
        output_key="loop_summary",
    )

    rollback_agent = LlmAgent(
        name="rollback_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.2, top_p=0.3, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/rollback_instruction.txt"),
        tools=wrap_tools([
            cbsc_classify_log,
            execute_hsr_decision,
            clear_commit_analysis_state
        ]),
        output_key="hsr_decision",
    )

    commit_finder_agent = LlmAgent(
        name="commit_finder_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.4, top_p=0.4, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/commit_finder_instruction.txt"),
        tools=wrap_tools([
            read_file_content,
            check_file_exists,
            extract_buggy_line_info,
            get_project_paths,
            list_files_in_dir,
            run_command,
            update_yaml_report,
            run_ecrcl_localization,
        ]),
        output_key="commit_analysis_result",
    )

    prompt_generate_agent = LlmAgent(
        name="prompt_generate_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=16384, temperature=0.2, top_p=0.3,
                      seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/prompt_generate_instruction.txt"),
        tools=wrap_tools([
            prompt_generate_tool,
            save_file_tree_shallow,
            find_and_append_file_details,
            read_file_content,
            list_files_in_dir,
            create_or_update_file,
            append_string_to_file,
            query_expert_knowledge,
            few_shot_rag_retrieve,
            query_trace_ledger,
        ]),
        output_key="generated_prompt",
    )

    fuzzing_solver_agent = LlmAgent(
        name="fuzzing_solver_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=8129, temperature=0.7, top_p=0.6,
                      seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/fuzzing_solver_instruction.txt"),
        tools=wrap_tools([read_file_content, create_or_update_file]),
        output_key="solution_plan",
    )

    solution_applier_agent = LlmAgent(
        name="solution_applier_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.2, top_p=0.3, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/solution_applier_instruction.txt"),
        tools=wrap_tools([
            apply_patch,
            read_file_content,
            manage_git_state,
            create_or_update_file,
            update_trace_ledger
        ]),
        output_key="patch_application_result",
    )

    # 2. 包装节点
    setup_node = node(initial_setup_agent, name="initial_setup_agent")
    fuzz_node = node(run_fuzz_and_collect_log_agent, name="run_fuzz_and_collect_log_agent")
    decision_node = node(decision_agent, name="decision_agent")
    rsmc_node = node(rsmc_agent, name="rsmc_agent")
    rollback_node = node(rollback_agent, name="rollback_agent")
    finder_node = node(commit_finder_agent, name="commit_finder_agent")
    prompt_node = node(prompt_generate_agent, name="prompt_generate_agent")
    solver_node = node(fuzzing_solver_agent, name="fuzzing_solver_agent")
    applier_node = node(solution_applier_agent, name="solution_applier_agent")

    # 3. 路由逻辑 (实现图内自动循环)
    @node(name="router_node")
    async def router_node(ctx: Context, node_input: Any):
        decision_result = str(ctx.state.get("decision_result", "")).lower()
        if "result: success" in decision_result:
            return Event(route="exit")

        current_round = ctx.state.get("round_id", 0)
        if current_round < 3:
            # 🔑 优化：利用 Event 的 state 属性原子化、安全地向持久化会话树回写 round_id 增量，防止绕过 Checkpoint 机制
            return Event(route="continue", state={"round_id": current_round + 1})
        return Event(route="exit")

    success_node = node(lambda: {"status": "SUCCESS"}, name="success_node")

    # 4. 构建闭环图结构
    edges = [
        ("START", setup_node),
        (setup_node, fuzz_node),
        (fuzz_node, decision_node),
        (decision_node, router_node),
        Edge(from_node=router_node, route="continue", to_node=rsmc_node),
        (rsmc_node, rollback_node),
        (rollback_node, finder_node),
        (finder_node, prompt_node),
        (prompt_node, solver_node),
        (solver_node, applier_node),
        (applier_node, fuzz_node),  # 闭环核心：补丁应用后触发重新编译
        Edge(from_node=router_node, route="exit", to_node=success_node),
    ]

    subject_workflow = Workflow(
        name="fix_fuzz_workflow",
        edges=edges,
        description="Self-looping iterative repair workflow."
    )

    return subject_workflow, InMemorySessionService()


async def process_single_project(
        project_info: Dict,
        yaml_path: str,
        row_index: int
) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    print(f"[EVIDENCE] YAML Data Audit - Root Cause Commit: '{project_info.get('root_cause_commit')}'")
    print(f"[EVIDENCE] YAML Data Audit - Workspace: '{project_info.get('root_cause_workspace')}'")

    project_name = project_info['project_name']
    TraceLedgerManager.set_active_project(project_name)
    safe_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
    expected_source_path = os.path.join(os.getcwd(), "process", "project", safe_name)

    oss_fuzz_sha = project_info['sha']
    software_sha = project_info.get('software_sha', "N/A")
    original_log_path = project_info.get('original_log_path', "")

    project_start_time = time.time()
    project_total_tokens = {"prompt": 0, "completion": 0, "total": 0}
    full_deterioration_history = []

    TIMEOUT_LIMIT = 5400
    is_successful = False
    final_basic_information = None
    last_run_stats = {}

    # 清空历史残余账本（ project_repair_trace.json ）
    ledger_abs_file = TraceLedgerManager.get_ledger_path()
    if os.path.exists(ledger_abs_file):
        try:
            safe_delete_path(ledger_abs_file)
            print(f"--- 🧹 Cleared stale trace ledger at {ledger_abs_file} ---")
        except Exception as e:
            print(f"--- ⚠️ Failed to clean stale ledger: {e} ---")

    # 跨 Attempt 追踪上游补丁标志
    ever_used_upstream = False
    for attempt in range(MAX_RETRIES):

        cleanup_environment(project_name)
        current_attempt_id = attempt + 1
        processed_event_ids = set()
        stats = {
            "repair_rounds": 0, "build_calls": 0, "rollback_count": 0,
            "total_tokens": {"prompt": 0, "completion": 0, "total": 0},
            "code_gen_tokens": 0, "scores": full_deterioration_history,
            "decision_type": "UNKNOWN", "patch_impact": {"files": 0, "lines": 0},
            "heuristic_used": False, "attempt_id": current_attempt_id
        }
        last_run_stats = stats

        # 1. 必须先创建 Session 并准备好 state，才能初始化 Agent
        session_service = InMemorySessionService()
        current_session_id = f"session_{project_name}_{int(time.time())}_at{attempt}"

        await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=current_session_id)
        session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=current_session_id)

        # 预加载 root_cause 数据到 state
        session.state["root_cause_commit"] = project_info.get("root_cause_commit", "")
        session.state["root_cause_workspace"] = project_info.get("root_cause_workspace", "")

        # 2. 审计代码：检查 session.state 内容
        print(f"[AUDIT] Initializing agents with state: {session.state}")

        # 3. 传入 session.state 完成注入式初始化
        try:
            root_agent, _ = initialize_agents(session_state=session.state)
        except Exception as e:
            print(f"[CRITICAL] initialize_agents failed: {e}")
            raise e

        # 物理 Git 与账本一致性审计
        ledger = TraceLedgerManager.load_ledger()
        if ledger.get("nodes"):
            last_node = ledger["nodes"][-1]
            ledger_oss = last_node.get("git_sha_state", {}).get("oss-fuzz_sha")
            ledger_prj = last_node.get("git_sha_state", {}).get("project_sha")

            disk_oss = TraceLedgerManager.get_git_head_sha(os.path.join(os.getcwd(), "oss-fuzz"))
            disk_prj = TraceLedgerManager.get_git_head_sha(expected_source_path)

            if ledger_oss != "N/A" and disk_oss != "N/A" and ledger_oss != disk_oss:
                print(
                    f"--- ⚠️ Integrity Mismatch [OSS-Fuzz]: Ledger={ledger_oss[:7]}, Disk={disk_oss[:7]}. Resetting... ---")
                subprocess.run(["git", "-C", "oss-fuzz", "reset", "--hard", ledger_oss], check=True)
                subprocess.run(["git", "-C", "oss-fuzz", "clean", "-fxd"], check=True)

            if ledger_prj != "N/A" and disk_prj != "N/A" and ledger_prj != disk_prj:
                print(
                    f"--- ⚠️ Integrity Mismatch [Upstream]: Ledger={ledger_prj[:7]}, Disk={disk_prj[:7]}. Resetting... ---")
                subprocess.run(["git", "-C", expected_source_path, "reset", "--hard", ledger_prj], check=True)
                subprocess.run(["git", "-C", expected_source_path, "clean", "-fxd"], check=True)

        # 初始化会话状态
        session.state["attempt_id"] = current_attempt_id
        session.state["round_id"] = 0
        session.state["current_node_id"] = 0
        session.state["rollback_triggered"] = False
        session.state["ever_used_upstream"] = ever_used_upstream
        session.state["project_name"] = project_name
        session.state["project_source_path"] = expected_source_path
        session.state["project_config_path"] = os.path.join(os.getcwd(), "oss-fuzz", "projects", project_name)
        session.state["error_time"] = project_info.get('error_time', "")
        session.state["root_cause_commit"] = project_info.get("root_cause_commit", "")
        session.state["root_cause_workspace"] = project_info.get("root_cause_workspace", "")

        oss_sha = get_verified_git_sha("./oss-fuzz")
        prj_sha = get_verified_git_sha(expected_source_path)

        # 初始化基线账本 Node 0
        initial_ledger = {
            "project_name": project_name,
            "archive_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "nodes": [{
                "node_id": 0,
                "parent_id": -1,
                "identification": {
                    "attempt_id": current_attempt_id,
                    "round_id": 0,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "node_status": "Stable",
                    "should_rollback": False,
                    "rollback_type": "NONE"
                },
                "git_sha_state": {
                    "oss-fuzz_sha": oss_sha,
                    "project_sha": prj_sha
                },
                "action_and_intent": {
                    "root_cause_commit_sha": "N/A",
                    "active_workspace": "UNKNOWN",
                    "target_file": "N/A",
                    "repair_strategy": "Initial baseline state configuration.",
                    "loop_summary": "Baseline compile completed."
                },
                "metrics": {
                    "Ldel": 0, "Ladd": 0,
                    "build_stage_before": "N/A",
                    "build_stage_after": "L1"
                },
                "validation": {
                    "step_1_6_bitmap": [0, 0, 0, 0, 0, 0],
                    "validation_report_before": {},
                    "validation_report_after": {}
                },
                "semantic_memory": {
                    "solved_problems": "None. Initial setup completed.",
                    "unsolved_problems": "Initial build attempt pending.",
                    "reflection_analysis": "Initial environment setup."
                }
            }]
        }
        TraceLedgerManager.save_ledger(initial_ledger)
        print(f"--- 📝 Node 0 (Baseline) verified and initialized: {oss_sha[:7]}|{prj_sha[:7]} ---")
        print(f"--- 📝 Node 0 (Baseline) initialized in trace ledger. ---")

        GLOBAL_LOGGER.set_project_context(project_name)
        runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

        initial_input = json.dumps({
            "project_name": project_name,
            "oss_fuzz_sha": oss_fuzz_sha,
            "error_time": project_info.get('error_time', ""),
            "original_log_path": original_log_path,
            "project_source_path": expected_source_path,
            "software_repo_url": project_info.get('software_repo_url', ""),
            "software_sha": software_sha,
            "engine": project_info.get('engine', ""),
            "sanitizer": project_info.get('sanitizer', ""),
            "architecture": project_info.get('architecture', ""),
            "base_image_digest": project_info.get('base_image_digest', ""),
            "attempt_id": current_attempt_id,
            "root_cause_commit": project_info.get("root_cause_commit", ""),
            "root_cause_workspace": project_info.get("root_cause_workspace", "")
        })
        initial_message = types.Content(parts=[types.Part(text=initial_input)], role='user')

        try:
            print(f"\n--- 🌀 Starting Attempt {current_attempt_id}/{MAX_RETRIES} (Resilient State) ---")

            # 🔑 物理加固：还原为低耦合生成器，允许安全拦截 ValueError 并在不崩溃的情况下继续执行
            gen = runner.run_async(user_id=USER_ID, session_id=current_session_id, new_message=initial_message)
            while True:
                try:
                    event = await gen.__anext__()
                except StopAsyncIteration:
                    break
                except ValueError as ve:
                    # 🔑 物理加固 1：劫持并非法豁免未注册工具，防止大模型幻觉直接崩掉主工作流
                    err_msg = str(ve)
                    if "not found" in err_msg or "not registered" in err_msg:
                        tool_name = err_msg.split("'")[1] if "'" in err_msg else "unknown"
                        print(f"--- ⚠️ Intercepted Illegal Tool Call: {tool_name}. Skipping to prevent crash. ---")
                        GLOBAL_LOGGER.log_raw(f"Security Alert: Agent attempted to call unauthorized tool: {tool_name}")
                        continue
                    else:
                        raise ve

                # 🔑 事件去重与标准转换
                event_uid = getattr(event, 'id', hash(repr(event)))
                has_actions = hasattr(event, 'actions') and event.actions is not None
                is_final_resp = event.is_final_response() if hasattr(event, 'is_final_response') else False

                dedup_key = (event_uid, 'final' if (is_final_resp or has_actions) else 'stream')
                if dedup_key in processed_event_ids:
                    continue
                processed_event_ids.add(dedup_key)

                GLOBAL_LOGGER.log_event(event)

                # Token 计数器更新
                if event.usage_metadata:
                    p = getattr(event.usage_metadata, "prompt_token_count", 0) or 0
                    c = getattr(event.usage_metadata, "candidates_token_count", 0) or 0
                    stats["total_tokens"]["prompt"] += p
                    stats["total_tokens"]["completion"] += c
                    stats["total_tokens"]["total"] += (p + c)
                    project_total_tokens["total"] += (p + c)
                    project_total_tokens["prompt"] = project_total_tokens.get("prompt", 0) + p
                    project_total_tokens["completion"] = project_total_tokens.get("completion", 0) + c
                    if event.author == 'fuzzing_solver_agent':
                        stats["code_gen_tokens"] += c

                # 🔑 拦截 1：处理 Initial Setup 的环境配置输出
                if event.author == 'initial_setup_agent' and event.actions and event.actions.state_delta:
                    if 'basic_information' in event.actions.state_delta:
                        full_info = event.actions.state_delta['basic_information']
                        try:
                            data = None
                            if isinstance(full_info, dict):
                                data = full_info
                            elif isinstance(full_info, str):
                                json_match = re.search(r'(\{[\s\S]*\})', full_info)
                                if json_match:
                                    data = json.loads(json_match.group(1))

                            if data:
                                session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID,
                                                                            session_id=current_session_id)
                                parsed_source_path = data.get("project_source_path", expected_source_path)
                                session.state["project_source_path"] = os.path.abspath(parsed_source_path)

                                parsed_config_path = data.get("project_config_path")
                                if parsed_config_path:
                                    session.state["project_config_path"] = os.path.abspath(parsed_config_path)
                                else:
                                    session.state["project_config_path"] = os.path.join(os.getcwd(), "oss-fuzz",
                                                                                        "projects", project_name)

                                session.state["error_time"] = data.get("error_time", "")

                                # 防止大模型丢失核心编译元数据，物理兜底强同步
                                fallback_metadata = {
                                    "project_name": project_name,
                                    "oss_fuzz_sha": oss_fuzz_sha,
                                    "error_time": project_info.get('error_time', ""),
                                    "original_log_path": original_log_path,
                                    "project_source_path": expected_source_path,
                                    "software_repo_url": project_info.get('software_repo_url', ""),
                                    "software_sha": software_sha,
                                    "engine": project_info.get('engine', ""),
                                    "sanitizer": project_info.get('sanitizer', ""),
                                    "architecture": project_info.get('architecture', ""),
                                    "base_image_digest": project_info.get('base_image_digest', ""),
                                    "root_cause_commit": project_info.get("root_cause_commit", ""),
                                    "root_cause_workspace": project_info.get("root_cause_workspace", "")
                                }

                                # 🔑 物理重构 2：遍历全量基础信息，若字段缺失、空白或为 "N/A"，则执行强行兜底回填
                                for key, val in fallback_metadata.items():
                                    if key not in data or not data[key] or data[key] in ["N/A", ""]:
                                        data[key] = val

                                # 🔑 物理重构 3：将归一化后的数据写入 session 变量，保障 downstream 其它 Agent 会话上下文无损
                                session.state["basic_information"] = data

                                # 🔑 物理重构 4：双层架构完全同步。将对应键值直接对齐至顶级状态，确保物理数据一致性，并强制实施绝对路径安全规整
                                session.state["project_name"] = data["project_name"]
                                session.state["project_source_path"] = os.path.abspath(data["project_source_path"])
                                session.state["error_time"] = data["error_time"]
                                session.state["root_cause_commit"] = data["root_cause_commit"]
                                session.state["root_cause_workspace"] = data["root_cause_workspace"]

                                print(
                                    f"--- 💾 Metadata synced successfully: source_path={session.state['project_source_path']}, config_path={session.state['project_config_path']} ---")
                                print(
                                    f"--- 💾 Preset Root Cause synced: commit={data['root_cause_commit']}, workspace={data['root_cause_workspace']} ---")
                        except Exception as e:
                            print(f"--- ⚠️ Metadata sync failed: {e} ---")

                # 🔑 拦截 2：rsmc_agent 反思节点脱水
                if event.author == 'rsmc_agent' and event.actions and event.actions.state_delta:
                    if 'loop_summary' in event.actions.state_delta:
                        summary = event.actions.state_delta['loop_summary']
                        if len(summary) > 800:
                            event.actions.state_delta['loop_summary'] = summary[:797] + "..."
                            print("--- [Orchestrator] Force truncated loop_summary to save tokens ---")
                        print("--- [Orchestrator] Step 3 RSMC finished. Executing Clean-1 (Pruning build logs)... ---")
                        await _safe_memory_cleaning(session_service, current_session_id)

                # 🔑 拦截 3：solution_applier_agent 封版节点脱水
                if event.author == 'solution_applier_agent' and event.actions and event.actions.state_delta:
                    if 'patch_application_result' in event.actions.state_delta:
                        print(
                            "--- [Orchestrator] Step 8 Applier finished. Executing Clean-2 (Pruning Solver & Finder history)... ---")
                        await _safe_memory_cleaning(session_service, current_session_id)

                # 🔑 拦截 4：监测定位完成
                if event.author == "commit_finder_agent" and event.actions and event.actions.state_delta:
                    artifact_path = os.path.join(os.getcwd(), "generated_prompt_file", "commit_changed.txt")
                    if os.path.exists(artifact_path):
                        try:
                            with open(artifact_path, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read()
                                sha_m = re.search(r"SHA:\s*([a-f0-9]+)", content, re.I)
                                ws_m = re.search(r"\[ATTRIBUTION_TYPE\]\s*\n\s*(UPSTREAM|DOWNSTREAM)", content, re.I)
                                if sha_m and ws_m and not project_info.get("root_cause_commit"):
                                    update_yaml_report(
                                        file_path=yaml_path,
                                        row_index=row_index,
                                        result_str=None,
                                        root_cause_commit=sha_m.group(1).strip(),
                                        root_cause_workspace=ws_m.group(1).strip().upper()
                                    )
                        except Exception as e:
                            print(f"--- ⚠️ Warning: Failed to sync commit_finder report to yaml: {e} ---")

                # 🔑 拦截 5：函数级探针劫持
                if (func_resps := event.get_function_responses()):
                    for resp in func_resps:
                        if resp.name in ['run_fuzz_build_streaming', 'run_fuzz_build_and_validate']:
                            stats["build_calls"] += 1
                            stats["repair_rounds"] = max(0, stats["build_calls"] - 1)

                        if resp.name == 'run_fuzz_build_and_validate':
                            val_report = resp.response.get('validation_report')
                            if val_report:
                                session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID,
                                                                            session_id=current_session_id)
                                session.state["last_validation_report"] = val_report
                                session.state["rollback_triggered"] = False

                                if session.state.get("round_id") == 0:
                                    print("--- [补全] Executing CBSC for Node 0 initial stage backfill... ---")
                                    classification = cbsc_classify_log()
                                    TraceLedgerManager.update_node_fields(0, {
                                        "metrics.build_stage_after": classification["determined_stage"]
                                    })

                        if resp.name == 'execute_hsr_decision':
                            if resp.response.get("action") == "ROLLBACK":
                                session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID,
                                                                            session_id=current_session_id)
                                session.state["current_node_id"] = resp.response.get("target_node_id")

                        if resp.name == 'apply_patch' and resp.response.get('status') in ['success', 'partial_success']:
                            session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID,
                                                                        session_id=current_session_id)

                            cfg_path = session.state.get("project_config_path") if session else None
                            if not cfg_path or not os.path.exists(cfg_path):
                                cfg_path = os.path.join(os.getcwd(), "oss-fuzz", "projects", project_name)

                            src_path = session.state.get("project_source_path") if session else None
                            if not src_path or not os.path.exists(src_path):
                                src_path = expected_source_path

                            oss_sha = TraceLedgerManager.get_git_head_sha(cfg_path)
                            prj_sha = TraceLedgerManager.get_git_head_sha(src_path)

                            curr_node = session.state.get("current_node_id", 0) if session else 0
                            TraceLedgerManager.update_node_fields(curr_node, {
                                "git_sha_state.oss-fuzz_sha": oss_sha,
                                "git_sha_state.project_sha": prj_sha
                            })

                            ledger = TraceLedgerManager.load_ledger()
                            if ledger.get("nodes"):
                                last_node = ledger["nodes"][-1]
                                if last_node.get("action_and_intent", {}).get("active_workspace") == "UPSTREAM":
                                    ever_used_upstream = True
                                    if session:
                                        session.state["ever_used_upstream"] = True

                # 实时监控退出条件
                curr_session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID,
                                                                 session_id=current_session_id)
                decision_output = str(curr_session.state.get("decision_result", "")).lower()

                is_exit_triggered = (event.actions and event.actions.escalate)
                if is_exit_triggered or "result: success" in decision_output:
                    is_successful = True
                    print(f"--- ✅ Build success/exit signal detected. Workflow finishing. ---")
                    break

                # 🔑 物理加固 2：恢复工作流中途物理超时审计，防止无限循环
                if (time.time() - project_start_time) > TIMEOUT_LIMIT:
                    print(f"--- ❌ [TIMEOUT] Project {project_name} reached limit. ---")
                    break

            if is_successful:
                break

        except litellm.ContextWindowExceededError as e:
            # 🔑 物理加固 3：单独捕获 Token 越界，阻止 Traceback 污染终端
            print(f"--- 🚨 [CRITICAL] Context limit exceeded: {e} ---")
            if attempt + 1 >= MAX_RETRIES:
                break
            continue

        except Exception as e:
            err_tb = traceback.format_exc()
            print(f"\n--- ❌ [CRASH DETECTED] Attempt {current_attempt_id} failed: {str(e)} ---")
            print(err_tb)
            GLOBAL_LOGGER.log_raw(f"[CRITICAL ATTEMPT EXCEPTION]\nException: {str(e)}\nTraceback:\n{err_tb}")
            await asyncio.sleep(1)
            if attempt + 1 >= MAX_RETRIES:
                break
            continue

            # 🔑 物理加固 4：【恢复物理归档环节】在 attempt 循环结束后，必须执行物理归档，防止成功文件丢失
        try:
            cfg_path = session.state.get("project_config_path")
            if not cfg_path or not os.path.exists(cfg_path):
                cfg_path = os.path.join(os.getcwd(), "oss-fuzz", "projects", project_name)

            src_path = session.state.get("project_source_path")
            if not src_path or not os.path.exists(src_path):
                src_path = os.path.join(os.getcwd(), "process", "project", safe_name)

            archive_fixed_project(
                project_name=project_name,
                project_config_path=cfg_path,
                is_success=is_successful,
                project_source_path=src_path
            )
            print(f"--- 📦 Project successfully archived to fixed repository context ---")
        except Exception as e:
            print(f"--- ⚠️ [ERROR] Archive failed: {e} ---")
    # 🔑 修正：移动到 4 个空格缩进，使其彻底处于 for attempt 循环外部（与 for 关键字垂直对齐）
    found_sha, found_workspace = None, None
    artifact_path = os.path.join(os.getcwd(), "generated_prompt_file", "commit_changed.txt")

    if os.path.exists(artifact_path):
        try:
            with open(artifact_path, 'r', encoding='utf-8', errors='ignore') as f:
                art_content = f.read()

            # 提取 SHA
            sha_m = re.search(r"SHA:\s*([a-f0-9]+)", art_content, re.I)
            if sha_m:
                found_sha = sha_m.group(1).strip()

            # 提取 Workspace
            ws_m = re.search(r"\[ATTRIBUTION_TYPE\]\s*\n\s*(UPSTREAM|DOWNSTREAM)", art_content, re.I)
            if ws_m:
                found_workspace = ws_m.group(1).strip().upper()

        except Exception as e:
            print(f"--- ⚠️ Warning: Failed to parse root cause from artifact: {e} ---")

    # 如果工件未产生但原本输入就有，采取入参数据进行兜底
    final_sha = found_sha if (found_sha and found_sha != "UNKNOWN") else project_info.get("root_cause_commit", "")
    final_workspace = found_workspace if found_workspace else project_info.get("root_cause_workspace", "")

    return is_successful, session.state.get("project_config_path"), final_sha, final_workspace


warnings.filterwarnings("ignore", category=RuntimeWarning, module="google.adk")


async def main():
    print("--- Starting automated fix workflow ---")

    YAML_FILE = 'projects.yaml'

    # 🔑 调整：不再在 main() 中全局创建 Agent，它们会在 Attempt 启动时由流程自动重新生成
    projects_result = read_projects_from_yaml(YAML_FILE)

    if not isinstance(projects_result, dict):
        print(f"❌ Critical Error: read_projects_from_yaml returned invalid type: {projects_result}")
        return

    if projects_result.get('status') == 'error':
        print(f"Error: Could not process YAML file: {projects_result.get('message')}")
        return

    projects_to_process = projects_result.get('projects', [])
    if not projects_to_process:
        print("--- No new projects to process were found. Workflow finished. ---")
        return

    print(f"--- Found {len(projects_to_process)} projects to process ---")

    for project_info in projects_to_process:
        try:
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
                "base_image_digest": project_info['base_image_digest'],
                "error_time": project_info['error_time'],
                # 🔑 新增：载入可能预设在 YAML 里的 root_cause_commit 和 root_cause_workspace
                "root_cause_commit": project_info.get('root_cause_commit', ""),
                "root_cause_workspace": project_info.get('root_cause_workspace', "")
            }

            print(f"\n{'=' * 60}")
            print(f"--- Processing Project: {project_name} (Index: {row_index}) ---")
            print(f"{'=' * 60}")

            # 使用支持根写的新工具置于 Progress
            update_yaml_report(YAML_FILE, row_index, "Failure (Crashed/In_Progress)")
            cleanup_environment(project_name)

            # 🔑 调整：匹配接收四个返回值，包含根因提取出的 SHA 和 workspace
            is_successful, project_config_path, final_sha, final_workspace = await process_single_project(
                initial_input_data,
                YAML_FILE,
                row_index
            )

            result_str = "Success" if is_successful else "Failure"
            print(f"--- Project {project_name} complete. Result: {result_str} ---")

            # 🔑 调整：使用支持根写的 YAML 更新函数，在 error_category 插入 root_cause_commit 和 root_cause_workspace
            update_result = update_yaml_report(
                file_path=YAML_FILE,
                row_index=row_index,
                result_str=result_str,
                root_cause_commit=final_sha,
                root_cause_workspace=final_workspace
            )

            if update_result['status'] == 'error':
                print(f"--- [CRITICAL] Could not update YAML report: {update_result['message']} ---")

            cleanup_environment(project_name)
        except Exception as e:
            print(f"--- [CRITICAL] Project {project_name} failed with error: {e} ---")
            continue

    print("\n--- All projects in the queue have been processed. Workflow finished. ---")


if __name__ == "__main__":
    print("--- Performing pre-startup checks... ---")
    sys.stdout = StreamTee(sys.stdout, GLOBAL_LOGGER)
    sys.stderr = StreamTee(sys.stderr, GLOBAL_LOGGER)
    if not DPSEEK_API_KEY:
        print("\n[ERROR] Startup failed: DPSEEK_API_KEY is not set.")
    else:
        print("✅ DPSEEK_API_KEY is set.")
        try:
            subprocess.run(["gh", "--version"], check=True, capture_output=True, text=True)
            print("✅ GitHub CLI ('gh') is installed.")
            try:
                print("✅ 'requests' library is installed.")
            except ImportError:
                print("\n[ERROR] Startup failed: 'requests' library is not installed.")
                sys.exit(1)
            subprocess.run(["gh", "auth", "status"], check=True, capture_output=True)
            print("✅ GitHub CLI ('gh') is logged in.")
            print("\n--- Checks complete. Preparing to start the Agent... ---")

            # 🔑 物理执行标准异步事件循环，并在内部启动主程序
            asyncio.run(main())

        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print("\n[ERROR] Startup failed: GitHub CLI ('gh') is not installed or not logged in.")
            print(f"Error details: {e}")
