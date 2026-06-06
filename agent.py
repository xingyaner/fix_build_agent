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
from google.adk.agents import LoopAgent, LlmAgent, BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.genai import types

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
    subject_agent: BaseAgent

    async def _run_async_impl(self, context: InvocationContext) -> AsyncGenerator[Event, None]:
        try:
            async for event in self.subject_agent.run_async(context):
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


def update_trace_ledger(node_id: int, fields_dict: dict, tool_context: ToolContext = None) -> dict:
    """
    Secure backfilling tool for the Solution Applier Agent.
    Safely writes file diff metrics, active workspace, and Git SHAs into project_repair_trace.json.
    """
    from agent_tools import TraceLedgerManager
    try:
        success = TraceLedgerManager.update_node_fields(node_id, fields_dict)
        if success:
            return {"status": "success", "message": f"Node {node_id} fields successfully backfilled."}
        else:
            return {"status": "error", "message": f"Failed to backfill Node {node_id} fields."}
    except Exception as e:
        return {"status": "error", "message": f"Exception occurred during backfilling: {str(e)}"}


# =====================================================================
# 辅助函数：安全记忆清理与状态脱水 (物理手术完全无状态版)
# =====================================================================

async def _safe_memory_cleaning(session_service: InMemorySessionService, session_id: str):
    """
    【物理裁剪+原地覆盖版】会话记忆重组
    通过保留最少量的基准环境数据，彻底清除历史轮次的对话噪声，
    让下一次循环的所有智能体处于完全无状态，消除一切历史认知偏置与上下文膨胀。
    """
    session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    if not session or not session.events:
        return

    # 始终物理保留初始第一个 User Message (Index 0)
    new_events = [session.events[0]]

    # 仅保留 initial_setup_agent 产生的环境 baseline 引导事件
    for event in session.events[1:]:
        if event.author == 'initial_setup_agent':
            new_events.append(event)

    # --- 原地清空并覆盖事件列表 ---
    session.events.clear()
    for e in new_events:
        session.events.append(e)

    # 状态字典脱水，防止残留的大容量字符串挤爆 Context Window
    massive_keys = ["fuzz_build_log", "commit_analysis_result", "generated_prompt"]
    for key in massive_keys:
        if key in session.state:
            session.state[key] = f"[DEHYDRATED: SUMMARY IN LEDGER]"

    print(f"--- 🧼 [SAFE CLEANSED] Pruned session {session_id} events. All loop sub-agents reset to stateless. ---")
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


def initialize_agents() -> Tuple[BaseAgent, InMemorySessionService]:
    """
    Dynamically instantiates all agents and the InMemorySessionService within the
    active asyncio event loop to secure model socket connections.
    """
    initial_setup_agent = LlmAgent(
        name="initial_setup_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.2, top_p=0.3, seed=LLM_SEED),
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

    run_fuzz_and_collect_log_agent = LlmAgent(
        name="run_fuzz_and_collect_log_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.2, top_p=0.3, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/run_fuzz_and_collect_log_instruction.txt"),
        tools=[read_file_content, run_fuzz_build_and_validate],
        output_key="fuzz_build_log",
    )

    decision_agent = LlmAgent(
        name="decision_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.2, top_p=0.3, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/decision_instruction.txt"),
        tools=[read_file_content, exit_loop],
        output_key="decision_result",
    )

    # Step 3. rsmc_agent (RSMC 机制)
    # 🔑 修复：已彻底从 tool 列表中物理移除了 prune_session_history，改由 Orchestrator 进行高阶隐式重组
    rsmc_agent = LlmAgent(
        name="rsmc_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.4, top_p=0.6, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/rsmc_instruction.txt"),
        tools=[read_file_content, init_or_update_rsmc_ledger, query_trace_ledger],
        output_key="loop_summary",
    )

    # Step 4. rollback_agent (HSR 机制)
    rollback_agent = LlmAgent(
        name="rollback_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.2, top_p=0.3, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/rollback_instruction.txt"),
        tools=[
            cbsc_classify_log,
            execute_hsr_decision,
            clear_commit_analysis_state
        ],
        output_key="hsr_decision",
    )

    # Step 5. commit_finder_agent (ECRCL 机制)
    commit_finder_agent = LlmAgent(
        name="commit_finder_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.4, top_p=0.6, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/commit_finder_instruction.txt"),
        tools=[
            read_file_content,
            check_file_exists,
            run_command,
            get_project_paths,
            run_ecrcl_localization,
        ],
        output_key="commit_analysis_result",
    )

    # Step 6. prompt_generate_agent (Few-shot RAG 过滤与 Prompt 组装)
    prompt_generate_agent = LlmAgent(
        name="prompt_generate_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=16384, temperature=0.2, top_p=0.3,
                      seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/prompt_generate_instruction.txt"),
        tools=[
            prompt_generate_tool,
            save_file_tree_shallow,
            find_and_append_file_details,
            read_file_content,
            create_or_update_file,
            append_string_to_file,
            query_expert_knowledge,
            few_shot_rag_retrieve,
            query_trace_ledger,
        ],
        output_key="generated_prompt",
    )

    # Step 7. fuzzing_solver_agent
    fuzzing_solver_agent = LlmAgent(
        name="fuzzing_solver_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=8129, temperature=0.7, top_p=0.8,
                      seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/fuzzing_solver_instruction.txt"),
        tools=[read_file_content, create_or_update_file],
        output_key="solution_plan",
    )

    # Step 8. solution_applier_agent
    solution_applier_agent = LlmAgent(
        name="solution_applier_agent",
        model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, temperature=0.2, top_p=0.3, seed=LLM_SEED),
        instruction=load_instruction_from_file("instructions/solution_applier_instruction.txt"),
        tools=[
            apply_patch,
            read_file_content,
            manage_git_state,
            create_or_update_file,
            update_trace_ledger  # 🔑 已配备：本地安全的接力写盘工具
        ],
        output_key="patch_application_result",
    )

    # Orchestrated sequential sub-agents matching exact 8 sequential loops
    loop_sub_agents = [
        run_fuzz_and_collect_log_agent,
        decision_agent,
        rsmc_agent,
        rollback_agent,
        commit_finder_agent,
        prompt_generate_agent,
        fuzzing_solver_agent,
        solution_applier_agent
    ]

    workflow_loop_agent = LoopAgent(
        name="workflow_loop_agent",
        sub_agents=loop_sub_agents,
        max_iterations=8
    )

    subject_agent = SequentialAgent(
        name="fix_fuzz_agent",
        sub_agents=[initial_setup_agent, workflow_loop_agent],
        description="A workflow that automatically downloads, configures, and iteratively fixes Fuzzing build issues"
    )

    root_agent = LoggingWrapperAgent(subject_agent=subject_agent)
    session_service = InMemorySessionService()

    return root_agent, session_service


def cleanup_environment(project_name: str):
    import shutil
    import os
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
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                print(f"  - Cleaned: {path}")
            except Exception as e:
                print(f"  - Warning: Failed to clean {path}: {e}")


async def process_single_project(
        project_info: Dict
) -> Tuple[bool, Optional[str]]:
    project_name = project_info['project_name']
    TraceLedgerManager.set_active_project(project_name)

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
            os.remove(ledger_abs_file)
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

        # 🔑 核心改进：在每次 Attempt 刚启动时，彻底重新运行 initialize_agents() 进行认知重置！
        # 绝不让多轮大重试（Attempt）之间共享任何 Agent 的对话历史缓存或同一个内存数据库。
        root_agent, session_service = initialize_agents()

        current_session_id = f"session_{project_name}_{int(time.time())}_at{attempt}"
        await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=current_session_id)
        session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=current_session_id)

        # 确定本地项目代码库预期解压路径
        safe_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        expected_source_path = os.path.join(os.getcwd(), "process", "project", safe_name)

        # 初始化会话状态
        session.state["attempt_id"] = current_attempt_id
        session.state["round_id"] = 0
        session.state["current_node_id"] = 0
        session.state["rollback_triggered"] = False
        session.state["ever_used_upstream"] = ever_used_upstream

        # 提前预分配核心环境路径与初始时间，防止后续步骤因 KeyError 崩溃
        session.state["project_source_path"] = expected_source_path
        session.state["project_config_path"] = os.path.join(os.getcwd(), "oss-fuzz", "projects", project_name)
        session.state["error_time"] = project_info.get('error_time', "")

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
            "attempt_id": current_attempt_id
        })
        initial_message = types.Content(parts=[types.Part(text=initial_input)], role='user')

        try:
            print(f"\n--- 🌀 Starting Attempt {current_attempt_id}/{MAX_RETRIES} (Clean State) ---")

            async for event in runner.run_async(user_id=USER_ID, session_id=current_session_id,
                                                new_message=initial_message):
                event_uid = getattr(event, 'id', hash(repr(event)))

                # 🔑 核心逻辑 3：安全去重 Key 设计。通过区分流式文本事件与携带 actions / usage 的最终结算事件，
                # 避免去重机制误将后期的 state_delta 和 Token 计数吞掉 [1.2.5]
                has_actions = hasattr(event, 'actions') and event.actions is not None
                is_final_resp = event.is_final_response() if hasattr(event, 'is_final_response') else False

                dedup_key = (event_uid, 'final' if (is_final_resp or has_actions) else 'stream')
                if dedup_key in processed_event_ids: continue
                processed_event_ids.add(dedup_key)

                # Token 计数更新
                if event.usage_metadata:
                    p = getattr(event.usage_metadata, "prompt_token_count", 0) or 0
                    c = getattr(event.usage_metadata, "candidates_token_count", 0) or 0
                    stats["total_tokens"]["prompt"] += p
                    stats["total_tokens"]["completion"] += c
                    stats["total_tokens"]["total"] += (p + c)
                    project_total_tokens["total"] += (p + c)
                    project_total_tokens["prompt"] = project_total_tokens.get("prompt", 0) + p
                    project_total_tokens["completion"] = project_total_tokens.get("completion", 0) + c
                    if event.author == 'fuzzing_solver_agent': stats["code_gen_tokens"] += c

                if event.author == 'rsmc_agent' and event.actions and event.actions.state_delta:
                    if 'loop_summary' in event.actions.state_delta:
                        summary = event.actions.state_delta['loop_summary']
                        # 执行强制截断
                        if len(summary) > 800:
                            event.actions.state_delta['loop_summary'] = summary[:797] + "..."
                            print("--- [Orchestrator] Force truncated loop_summary to save tokens ---")
                        print("--- [Orchestrator] Step 3 RSMC finished. Executing Clean-1 (Pruning build logs)... ---")
                        await _safe_memory_cleaning(session_service, current_session_id)


                if event.author == 'solution_applier_agent' and event.actions and event.actions.state_delta:
                    if 'patch_application_result' in event.actions.state_delta:
                        print("--- [Orchestrator] Step 8 Applier finished. Executing Clean-2 (Pruning Solver & Finder history)... ---")
                        await _safe_memory_cleaning(session_service, current_session_id)


                if (func_resps := event.get_function_responses()):
                    for resp in func_resps:
                        if resp.name in ['run_fuzz_build_streaming', 'run_fuzz_build_and_validate']:
                            stats["build_calls"] += 1
                            stats["repair_rounds"] = max(0, stats["build_calls"] - 1)

                        # 拦截构建验证响应
                        if resp.name == 'run_fuzz_build_and_validate':
                            val_report = resp.response.get('validation_report')
                            if val_report:
                                session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID,
                                                                            session_id=current_session_id)
                                session.state["last_validation_report"] = val_report
                                session.state["rollback_triggered"] = False

                                # Node 0 阶段回填补丁 (处理时序死锁)
                                if session.state.get("round_id") == 0:
                                    print("--- [补全] Executing CBSC for Node 0 initial stage backfill... ---")
                                    classification = cbsc_classify_log()  # ✅ 由函数内部自动按优先级选择日志
                                    TraceLedgerManager.update_node_fields(0, {
                                        "metrics.build_stage_after": classification["determined_stage"]
                                    })

                        # 拦截 HSR 回退决策
                        if resp.name == 'execute_hsr_decision':
                            if resp.response.get("action") == "ROLLBACK":
                                session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID,
                                                                            session_id=current_session_id)
                                session.state["current_node_id"] = resp.response.get("target_node_id")

                        # 拦截补丁应用响应
                        if resp.name == 'apply_patch' and resp.response.get('status') in ['success', 'partial_success']:
                            stats["patch_impact"]["files"] = resp.response.get('modified_files_count', 0)
                            stats["patch_impact"]["lines"] = resp.response.get('total_lines_changed', 0)

                            # 更新挂载标志
                            ledger = TraceLedgerManager.load_ledger()
                            if ledger.get("nodes"):
                                last_node = ledger["nodes"][-1]
                                if last_node.get("action_and_intent", {}).get("active_workspace") == "UPSTREAM":
                                    ever_used_upstream = True
                                    session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID,
                                                                                session_id=current_session_id)
                                    session.state["ever_used_upstream"] = True

                            # 🔑 已经将 run_ecrcl_localization 作为 Tool 赋权给 commit_finder_agent 主动调用。
                            # 此处彻底删除 check_file_exists 的后置异步拦截拦截器，完全消除时序倒置与控制流 race condition。
                        if resp.name == 'clear_commit_analysis_state':
                            pass

                        # 处理 Initial Setup 输出

                # 处理 Initial Setup 输出
                if event.author == 'initial_setup_agent' and event.actions and event.actions.state_delta:
                    if 'basic_information' in event.actions.state_delta:
                        full_info = event.actions.state_delta['basic_information']
                        try:
                            # 🔑 核心逻辑 5：兼容 dict 与 str 多态结构，防止单引号强制强转引发的 JSON 反序列化崩溃
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
                                session.state["project_source_path"] = data.get("project_source_path",
                                                                                expected_source_path)
                                session.state["project_config_path"] = data.get("project_config_path")
                                session.state["error_time"] = data.get("error_time", "")
                                print(
                                    f"--- 💾 Metadata synced successfully: source_path={session.state['project_source_path']}, config_path={session.state['project_config_path']} ---")
                        except Exception as e:
                            print(f"--- ⚠️ Metadata sync failed: {e} ---")

                # 检查退出条件
                if (event.actions and event.actions.escalate and event.author == 'decision_agent'):
                    resps = event.get_function_responses()
                    if resps and resps[0].name == 'exit_loop' and resps[0].response.get('status') == 'SUCCESS':
                        is_successful = True

            if (time.time() - project_start_time) > TIMEOUT_LIMIT:
                print(f"--- ❌ [TIMEOUT] Project {project_name} reached limit. ---")
                break
            if is_successful: break

        except litellm.ContextWindowExceededError as e:
            print(f"--- 🚨 [CRITICAL] Context limit exceeded: {e} ---")
            if attempt + 1 >= MAX_RETRIES: break
            continue
        except Exception as e:
            print(f"--- ❌ [CRASH] Attempt {current_attempt_id} failed: {e} ---")
            if attempt + 1 >= MAX_RETRIES: break
            continue

    # 归档处理
    try:
        archive_fixed_project(
            project_name=project_name,
            project_config_path=session.state.get("project_config_path"),
            is_success=is_successful,
            project_source_path=os.path.join(os.getcwd(), "process", "project", safe_name)
        )
    except Exception as e:
        print(f"--- [ERROR] Archive failed: {e} ---")

    return is_successful, session.state.get("project_config_path")


# =====================================================================
# PART 4: Reconstructed main() and __main__ Entry Blocks
# 1. Calls the asynchronous agent initialization factory inside main()
#    to safely bind LiteLlm connection pools to the active event loop.
# 2. Passes the properly initialized root_agent instance dynamically.
# 3. Suppresses standard noisy warning filters if necessary.
# =====================================================================

import warnings

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

        print(f"\n{'=' * 60}")
        print(f"--- Processing Project: {project_name} (Index: {row_index}) ---")
        print(f"{'=' * 60}")

        update_yaml_report(YAML_FILE, row_index, "Failure (Crashed/In_Progress)")
        cleanup_environment(project_name)

        # 🔑 调整：调用时不再传递 root_agent 和 session_service
        is_successful, project_config_path = await process_single_project(
            initial_input_data
        )

        result_str = "Success" if is_successful else "Failure"
        print(f"--- Project {project_name} complete. Result: {result_str} ---")
        update_result = update_yaml_report(YAML_FILE, row_index, result_str)

        if update_result['status'] == 'error':
            print(f"--- [CRITICAL] Could not update YAML report: {update_result['message']} ---")

        cleanup_environment(project_name)

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
                import requests

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
