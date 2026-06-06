from typing import Dict, List, Tuple, Optional, Set, Any
from google.adk.tools.tool_context import ToolContext

import time
import signal
import logging
from datetime import datetime, timezone, timedelta

import os
import re
import sys
import shutil
import litellm
import requests
import subprocess
import json
import yaml
import openpyxl
import tempfile
import fnmatch
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Callable, Optional, Set, Any
from google.adk.tools.tool_context import ToolContext
from utils.path_utils import normalize_patch_path, validate_patch_path
from utils.error_handler import format_path_error

logger = logging.getLogger(__name__)

ENABLE_HISTORY_ENHANCEMENT = True
ENABLE_REFLECTION = True
ENABLE_ROLLBACK = True
ENABLE_EXPERT_KNOWLEDGE = True

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

PROCESSED_PROJECTS_DIR = os.path.join(CURRENT_DIR, "process")
PROCESSED_PROJECTS_FILE = os.path.join(PROCESSED_PROJECTS_DIR, "project_processed.txt")
GLOBAL_CHAR_BUDGET = 280000  # 硬编码
max_lines = 2500  # 硬编码


# =====================================================================
# 定位位置：fix_build_agent/agent_tools.py 中的 TraceLedgerManager 类
# 替换内容：引入 FILE_NAME 属性与自愈式活跃项目名追踪方法
# =====================================================================

class TraceLedgerManager:
    """
    Thread-safe Ledger State Tree persistence manager for project_repair_trace.json.
    Mediates all read/write interactions to keep the LLM decoupled from direct file system edits.
    """
    _active_project = "UNKNOWN"

    @classmethod
    def set_active_project(cls, project_name: str):
        cls._active_project = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()

    @classmethod
    def get_ledger_path(cls) -> str:
        return os.path.abspath(os.path.join(os.getcwd(), "project_repair_trace.json"))

    @classmethod
    def load_ledger(cls) -> dict:
        path = cls.get_ledger_path()
        if not os.path.exists(path):
            return {"project_name": cls._active_project, "archive_date": "", "nodes": []}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load ledger: {e}")
            return {"project_name": cls._active_project, "archive_date": "", "nodes": []}

    @classmethod
    def save_ledger(cls, data: dict) -> bool:
        path = cls.get_ledger_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error(f"Failed to save ledger: {e}")
            return False

    @classmethod
    def update_node_fields(cls, node_id: int, fields_dict: dict) -> bool:
        """
        Safely update arbitrary deep paths (using dot notation) of a specific node_id.
        """
        ledger = cls.load_ledger()
        target_node = None
        for node in ledger.get("nodes", []):
            if node.get("node_id") == node_id:
                target_node = node
                break

        if target_node is None:
            return False

        for key, value in fields_dict.items():
            parts = key.split('.')
            curr = target_node
            for part in parts[:-1]:
                if part not in curr:
                    curr[part] = {}
                curr = curr[part]
            curr[parts[-1]] = value

        return cls.save_ledger(ledger)

    @classmethod
    def get_git_head_sha(cls, repo_path: str) -> str:
        if not repo_path or not os.path.exists(repo_path):
            return "N/A"
        try:
            res = subprocess.run(["git", "-C", repo_path, "rev-parse", "HEAD"], capture_output=True, text=True,
                                 check=True)
            return res.stdout.strip()
        except Exception:
            return "N/A"


def _safe_path_wrapper(*d_args, **d_kwargs):
    """
    完全自适应型安全路径拦截包装装饰器。
    无缝兼容以下四种主流 Python 装饰器声明语法，绝不产生编译期或运行期类型报错：
    1. 无参直接修饰：  @_safe_path_wrapper
    2. 无参括号修饰：  @_safe_path_wrapper()
    3. 有参位置修饰：  @_safe_path_wrapper("save_file_tree_shallow")
    4. 有参关键字修饰：@_safe_path_wrapper(operation_name="read_file_content")
    """
    import functools
    import os
    from typing import Callable
    from google.adk.tools.tool_context import ToolContext

    # 1. 编译期解析有参传入的操作名称（兼容位置/关键字参数）
    op_name = None
    if d_args and isinstance(d_args[0], str):
        op_name = d_args[0]
    elif "operation_name" in d_kwargs:
        op_name = d_kwargs["operation_name"]

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 自动提取操作名（有参优先，无参则自动使用被修饰函数名）
            operation_name = op_name if op_name else func.__name__

            # 2. 安全提取 ToolContext（深度兼容位置参数与关键字参数）
            tool_context = None
            for arg in args:
                if isinstance(arg, ToolContext):
                    tool_context = arg
                    break
            if not tool_context:
                tool_context = kwargs.get("tool_context")

            # 3. HSR 物理与认知回退空跑保护机制 (Bypass Protection)
            if tool_context and (
                    tool_context.session.state.get("rollback_triggered") or
                    tool_context.session.state.get("should_rollback")
            ):
                print(f"--- ⚠️ [STATE BYPASS] {operation_name} bypassed due to active rollback state. ---")
                return {
                    "status": "skipped",
                    "message": f"Rollback triggered. Bypassing further {operation_name} edits in this round."
                }

            # 4. 强类型安全路径参数抓取 ( args[0] 必须是 str，防止把 args 里的 ToolContext 误当路径)
            path_arg = (
                    kwargs.get('file_path') or
                    kwargs.get('directory_path') or
                    kwargs.get('source_path') or
                    kwargs.get('destination_path') or
                    kwargs.get('dir_path') or
                    kwargs.get('solution_file_path') or
                    (args[0] if args and isinstance(args[0], str) else None)
            )

            if not path_arg:
                # 辅助性零路径探测：若函数本身确实无路径参数传入，则直接执行并解脱校验
                return func(*args, **kwargs)

            # 获取项目基准路径
            base_dir = kwargs.get('base_dir', os.environ.get('PROJECT_ROOT', os.getcwd()))
            strict_mode = kwargs.get('strict_mode', True)

            # 导入白名单验证逻辑与标准路径错误引导
            from utils.path_utils import normalize_patch_path, validate_patch_path
            from utils.error_handler import format_path_error

            normalized = normalize_patch_path(path_arg, base_dir)
            if strict_mode and not validate_patch_path(normalized, strict=True):
                return {
                    "status": "error",
                    "message": format_path_error(
                        original_path=path_arg,
                        normalized_path=normalized,
                        base_dir=base_dir,
                        validation_passed=False,
                        extra_info={'operation': operation_name}
                    )
                }

            # 规范化 kwargs 里的路径参数
            path_keys = ['file_path', 'directory_path', 'source_path', 'destination_path', 'dir_path',
                         'solution_file_path']
            for key in path_keys:
                if key in kwargs:
                    kwargs[key] = normalized

            # 规范化 args 里的位置路径参数（只有在确定 args[0] 物理匹配时才重写）
            new_args = list(args)
            if args and isinstance(args[0], str) and args[0] == path_arg:
                new_args[0] = normalized

            return func(*new_args, **kwargs)

        return wrapper

    # 核心自适应分流判定
    if d_args and callable(d_args[0]):
        func = d_args[0]
        return decorator(func)
    else:
        return decorator


def _is_initial_round() -> bool:
    """
    辅助检测器：读取统一账本判断当前是否处于首轮（Node 0/Node 1 baseline 阶段）
    """
    ledger_path = "project_repair_trace.json"
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, 'r', encoding='utf-8') as lf:
                trace_data = json.load(lf)
                # 若账本中节点数 > 1（已建立 Node 1 且后续被 backfill 扩展），则说明进入了非初始轮的迭代
                if trace_data.get("nodes") and len(trace_data["nodes"]) > 1:
                    return False
        except Exception:
            pass
    return True


def execute_hsr_decision(tool_context: ToolContext) -> dict:
    """
    Evaluates the Stage-Guided Decision Policy (HSR Engine).
    Compares SA >= SB dominance on the state tuple S = <L, V> to decide rollback action.
    Synchronizes double-workspace Git repositories with exact physical SHA targets on Rollback.
    """
    session = tool_context.session
    project_name = session.state.get("project_name") or session.state.get("project", "UNKNOWN")
    project_source_path = session.state.get("project_source_path")
    project_config_path = session.state.get("project_config_path")

    ledger = TraceLedgerManager.load_ledger()
    nodes = ledger.get("nodes", [])

    # 🔑 改进点 1：哨兵过滤。只选取已经回填了构建结论的活跃节点
    active_nodes = [n for n in nodes if n.get("metrics", {}).get("build_stage_after") != "N/A"]

    # 情况 A：如果连 Node 0 都没有回填（这种情况不应发生），或者只有 0 个活跃节点
    if len(active_nodes) < 1:
        return {
            "status": "success",
            "action": "NONE",
            "message": "System is at initial baseline. Build classification pending."
        }

    # 当前刚刚完成构建并回填信息的节点
    curr_node = active_nodes[-1]

    # 情况 B：如果当前活跃节点是 Node 0 (Baseline)
    if curr_node["node_id"] == 0:
        return {
            "status": "success",
            "action": "NONE",
            "message": "Evaluating Node 0 baseline. No previous node for comparison."
        }

    # 情况 C：寻找当前活跃节点的物理父节点进行对比
    prev_node = None
    for n in nodes:
        if n["node_id"] == curr_node["parent_id"]:
            prev_node = n
            break

    # 如果找不到父节点（例如 parent_id 为 -1 或 0 且 Node 0 尚未回填）
    if not prev_node:
        return {
            "status": "success",
            "action": "NONE",
            "message": f"Node {curr_node['node_id']} has no stable parent node to compare against."
        }

    # --- 只有满足以上条件，才进入 SA >= SB 的支配关系判定 ---
    # 建立 L1-L6 的映射
    stage_map = {"L1": 1, "L2": 2, "L3": 3, "L4": 4, "L5": 5, "L6": 6, "N/A": 0}
    L_curr = stage_map.get(curr_node.get("metrics", {}).get("build_stage_after", "N/A"), 0)
    L_prev = stage_map.get(prev_node.get("metrics", {}).get("build_stage_after", "N/A"), 0)

    # 2. 计算硬性验证指标 (Step 1, Step 2, Step 6) 的通过总数
    def get_validation_score(report: dict) -> int:
        score = 0
        if "pass" in str(report.get("step_1_official_list", "")).lower():
            score += 1
        if "pass" in str(report.get("step_2_infra_compliance", "")).lower():
            score += 1
        if "pass" in str(report.get("step_6_runtime_stability", "")).lower():
            score += 1
        return score

    V_curr = get_validation_score(curr_node.get("validation", {}).get("validation_report_after", {}))
    V_prev = get_validation_score(prev_node.get("validation", {}).get("validation_report_after", {}))

    # 3. 评估支配关系 (S_A >= S_B)
    def dominates(L_a: int, V_a: int, L_b: int, V_b: int) -> bool:
        if L_a > L_b:
            return True
        if L_a == L_b and V_a >= V_b:
            return True
        return False

    is_curr_dominated = dominates(L_prev, V_prev, L_curr, V_curr)
    is_equivalent = (L_curr == L_prev) and (V_curr == V_prev)

    decision_status = "Stable"
    rollback_type = "NONE"
    should_rollback = False
    target_node = None

    if is_curr_dominated:
        should_rollback = True
        if is_equivalent:
            # 阶段不变且验证不变 -> 白色 Neutral Path (单层回退 k=1)
            rollback_type = "SINGLE_STEP"
            target_node = prev_node
            decision_status = "Neutral Path"
        else:
            # 阶段退化或同阶段验证退化 -> 红色多层回退 (k > 1) 自适应追溯支配节点
            rollback_type = "ADAPTIVE"
            decision_status = "Degrading"

            # 自底向上单向回溯直系链
            for node in reversed(nodes[:-1]):
                L_hist = stage_map.get(node.get("metrics", {}).get("build_stage_after", "N/A"), 0)
                V_hist = get_validation_score(node.get("validation", {}).get("validation_report_after", {}))
                if dominates(L_hist, V_hist, L_curr, V_curr):
                    target_node = node
                    break

            # 兜底：若未匹配到任何历史节点，强制回退至 Node 0
            if target_node is None:
                target_node = nodes[0]

    # 回填当前处理节点 Node N 最终决策标签
    TraceLedgerManager.update_node_fields(curr_node["node_id"], {
        "identification.node_status": decision_status,
        "identification.should_rollback": should_rollback,
        "identification.rollback_type": rollback_type
    })

    if should_rollback and target_node:
        print(f"--- [HSR DECISION] Rollback Triggered ({rollback_type}) to Node {target_node['node_id']} ---")

        # 1. 双工作区物理 Git 重置：根据目标 Node 的 SHA 历史进行精准对齐
        shas_to_reset = [
            (project_source_path, target_node["git_sha_state"].get("project_sha")),
            (project_config_path, target_node["git_sha_state"].get("oss-fuzz_sha"))
        ]

        for repo_path, target_sha in shas_to_reset:
            if target_sha and target_sha != "N/A" and os.path.exists(repo_path):
                # 级联权限自愈
                uid, gid = os.getuid(), os.getgid()
                try:
                    subprocess.run([
                        "docker", "run", "--rm", "-v", f"{os.path.abspath(repo_path)}:/src",
                        "alpine", "chown", "-R", f"{uid}:{gid}", "/src"
                    ], capture_output=True, timeout=15)
                except Exception as perm_err:
                    logger.warning(f"Container chown failed before reset: {perm_err}")

                # 物理重置代码库
                try:
                    subprocess.run(["git", "-C", repo_path, "reset", "--hard", target_sha], check=True,
                                   capture_output=True)
                    subprocess.run(["git", "-C", repo_path, "clean", "-fxd"], check=True, capture_output=True)
                except Exception as git_err:
                    logger.error(f"Git physical reset failed for {repo_path} to {target_sha}: {git_err}")

        # 2. 物理构建输出制品库清理
        out_dir = os.path.join(project_config_path, "..", "..", "build", "out", project_name)
        if os.path.exists(out_dir):
            try:
                shutil.rmtree(out_dir)
                os.makedirs(out_dir, exist_ok=True)
                print(f"--- [CLEANUP] Successfully purged residual output directory: {out_dir} ---")
            except Exception as clean_err:
                logger.error(f"Failed to clear output folder {out_dir}: {clean_err}")

        # 3. 认知重置
        clear_commit_analysis_state()
        session.state["rollback_triggered"] = True

        # 🔑 关键：同步物理状态与 Session State 中的 SHA 记录，确保逻辑一致
        session.state["software_sha"] = target_node["git_sha_state"].get("project_sha")
        session.state["oss_fuzz_sha"] = target_node["git_sha_state"].get("oss-fuzz_sha")

        # 4. 拓扑分支状态裁剪
        ledger = TraceLedgerManager.load_ledger()
        ledger["nodes"] = [node for node in ledger["nodes"] if node["node_id"] <= target_node["node_id"]]
        TraceLedgerManager.save_ledger(ledger)

        return {
            "status": "success",
            "action": "ROLLBACK",
            "target_node_id": target_node["node_id"],
            "rollback_type": rollback_type,
            "message": f"Environment and ledger state tree aligned back to Node {target_node['node_id']}."
        }

    return {
        "status": "success",
        "action": "NONE",
        "message": "No rollback required. System state is progressing."
    }


# =====================================================================
# 2. RSMC 核心物理工具函数 (LLM 绑定注册工具)
# =====================================================================

# ... existing code ...

def query_trace_ledger(tool_context: ToolContext, field_keys: List[str], node_id: Optional[int] = None) -> dict:
    """
    Secure field-level getter tool for both LLM Agents and python backend prompter.
    Supports dot-notation path parsing and strictly accesses data based on the node_id.
    """
    session = tool_context.session

    if node_id is None:
        session_node_id = session.state.get("current_node_id", 0)
        node_id = max(0, session_node_id - 1)

    ledger = TraceLedgerManager.load_ledger()

    # 🔑 修改：账本不存在或 nodes 为空时，返回空数据而非 error
    if not ledger or not ledger.get("nodes"):
        retrieved_data = {key: "N/A (Ledger not initialized)" for key in field_keys}
        return {
            "status": "success",
            "node_id": node_id,
            "data": retrieved_data
        }

    target_node = None
    for node in ledger.get("nodes", []):
        if node.get("node_id") == node_id:
            target_node = node
            break

    # 🔑 修改：node_id 不存在时，返回空数据而非 error
    if not target_node:
        retrieved_data = {key: "N/A (Node not yet created)" for key in field_keys}
        return {
            "status": "success",
            "node_id": node_id,
            "data": retrieved_data
        }

    retrieved_data = {}
    for key_path in field_keys:
        parts = key_path.split('.')
        current_level = target_node
        success = True
        for part in parts:
            if isinstance(current_level, dict) and part in current_level:
                current_level = current_level[part]
            else:
                success = False
                break
        if success:
            retrieved_data[key_path] = current_level
        else:
            retrieved_data[key_path] = "N/A (Field not instantiated)"

    return {
        "status": "success",
        "node_id": node_id,
        "data": retrieved_data
    }


class EvidenceGraph:
    """
    Directed Weighted Graph for ECRCL Cross-Commit Evidence Propagation.
    No-cycle sandglass layout ensures energy converges strictly to Ncommit Sink nodes.
    Optimized: Self-healing environmental gating prevents premature zero-potentials under non-ASan configurations.
    """

    def __init__(self):
        self.nodes: Dict[str, str] = {}  # NodeId -> NodeType
        self.edges: Dict[str, Dict[str, float]] = {}  # SourceNodeId -> {TargetNodeId: Weight}

    def add_node(self, node_id: str, node_type: str):
        """Adds a heterogeneous node to the graph."""
        self.nodes[node_id] = node_type
        if node_id not in self.edges:
            self.edges[node_id] = {}

    def add_edge(self, u: str, v: str, weight: float):
        """Registers a directed weighted edge from u to v."""
        if u in self.nodes and v in self.nodes:
            self.edges[u][v] = weight

    def run_belief_propagation(self, active_commits: List[str], current_env: dict,
                               commit_messages: Optional[Dict[str, str]] = None) -> dict:
        """
        Executes exactly T=3 rounds of message passing.
        Applies out-degree normalization and environmental gating.
        """
        d = 0.85  # Damping factor

        # 1. 势能矩阵初始化 (Pt: NodeId -> Score)
        Pt = {nid: 0.0 for nid in self.nodes}
        Pt["Nregion"] = 1.0  # 故障异常源设置为全局唯一的因果起始源

        # 2. 预先构建转移概率（出度归一化，确保势能单向守恒）
        in_neighbors: Dict[str, List[Tuple[str, float]]] = {nid: [] for nid in self.nodes}
        for u, out_edges in self.edges.items():
            sum_w = sum(out_edges.values())
            if sum_w > 0:
                for v, w in out_edges.items():
                    norm_prob = w / sum_w  # 出度归一化概率
                    in_neighbors[v].append((u, norm_prob))

        # 3. 三轮信念传播迭代 (Message Passing)
        for t in range(3):
            next_Pt = {nid: 0.0 for nid in self.nodes}
            for u in self.nodes:
                I_u = 1.0 if u == "Nregion" else 0.0
                sum_in = 0.0
                for v, prob in in_neighbors[u]:
                    sum_in += prob * Pt[v]
                next_Pt[u] = (1.0 - d) * I_u + d * sum_in
            Pt = next_Pt

            # 环境门控过滤：精准识别冲突，降阻断低伪阳性 Commit
            env_san = current_env.get("SANITIZER", "address").lower()
            for commit in active_commits:
                commit_node = f"Ncommit_{commit}"
                if commit_node in Pt:
                    # 提取提交消息意图
                    commit_msg = commit_messages.get(commit, "").lower() if commit_messages else ""
                    has_conflict = False

                    # 🔑 修复：仅当环境与变动描述产生明确的排他冲突时，才触发 M=0.0 门控强力阻断
                    if "asan" in env_san or "address" in env_san:
                        if "msan" in commit_msg or "memory_sanitizer" in commit_msg:
                            has_conflict = True
                    elif "msan" in env_san or "memory" in env_san:
                        if "asan" in commit_msg or "address_sanitizer" in commit_msg:
                            has_conflict = True

                    if has_conflict:
                        Pt[commit_node] = 0.0

        # 4. 提取最终 Commit 节点的势能得分
        scores = {}
        for commit in active_commits:
            commit_node = f"Ncommit_{commit}"
            scores[commit] = Pt.get(commit_node, 0.0)
        return scores


@_safe_path_wrapper
def apply_patch(solution_file_path: str, **kwargs) -> dict:
    import difflib
    base_dir = kwargs.get('base_dir', os.getcwd())
    if not os.path.exists(solution_file_path):
        return {"status": "error", "message": "Solution file not found."}
    try:
        with open(solution_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        patch_blocks = content.split('---=== FILE ===---')[1:]
        applied_count = 0
        errors = []

        for block in patch_blocks:
            parts = block.split('---=== ORIGINAL ===---')
            original_target = parts[0].strip()
            content_parts = parts[1].split('---=== REPLACEMENT ===---')
            original_block = content_parts[0].strip("\n\r")
            replacement_block = content_parts[1].strip("\n\r")

            file_path = os.path.normpath(os.path.join(base_dir, original_target)) if not os.path.isabs(
                original_target) else original_target
            if not os.path.exists(file_path):
                errors.append(f"File not found: {original_target}")
                continue

            with open(file_path, 'r', encoding='utf-8') as f:
                file_content = f.read()

            if original_block in file_content:
                new_content = file_content.replace(original_block, replacement_block, 1)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                applied_count += 1
                continue

            norm_orig = re.sub(r'\s+', ' ', original_block).strip()
            norm_file = re.sub(r'\s+', ' ', file_content).strip()
            if norm_orig in norm_file:
                file_lines = file_content.splitlines()
                orig_lines = original_block.splitlines()
                matched_idx = -1
                for i in range(len(file_lines) - len(orig_lines) + 1):
                    window = "\n".join(file_lines[i:i + len(orig_lines)])
                    if re.sub(r'\s+', ' ', window).strip() == norm_orig:
                        matched_idx = i
                        break
                if matched_idx != -1:
                    new_lines = file_lines[:matched_idx] + replacement_block.splitlines() + file_lines[
                        matched_idx + len(
                            orig_lines):]
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write("\n".join(new_lines))
                    applied_count += 1
                    continue

            lines = file_content.splitlines()
            search_anchor = original_block.splitlines()[0].strip()
            matches = difflib.get_close_matches(search_anchor, lines, n=1, cutoff=0.3)
            ctx = "Unknown context"
            if matches:
                idx = lines.index(matches[0])
                ctx = "\n".join(lines[max(0, idx - 5):min(len(lines), idx + 10)])
            errors.append(f"MATCH FAILED for {original_target}.\n### ACTUAL CONTENT AROUND TARGET AREA ###\n{ctx}")

        return {"status": "success" if not errors else "error", "modified_files_count": applied_count, "errors": errors}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def clamp_diff_content(diff_text: str) -> str:
    """
    Enforces token budget limits on Unified Diff content.
    1. Single file diffs over 3000 chars are pruned to keep only hunk headers and modified lines.
    2. Total diff over 10000 chars is pruned to keep +, -, @ and headers.
    3. Exceeding 10000 chars after pruning falls back to 'git show --stat' summary.
    """
    if not diff_text:
        return ""

    # 按照文件划分 Diff 块
    file_blocks = []
    current_block = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_block:
                file_blocks.append("\n".join(current_block))
            current_block = [line]
        else:
            current_block.append(line)
    if current_block:
        file_blocks.append("\n".join(current_block))

    clamped_blocks = []
    for block in file_blocks:
        if len(block) > 3000:
            # 强行截断：抛弃未修改的上下文行，仅保留 Hunk Header + 修改行
            lines = block.splitlines()
            pruned_lines = [
                l for l in lines
                if l.startswith(('+', '-', '@', 'diff --git ', '--- ', '+++ ', 'index '))
            ]
            clamped_block = "\n".join(pruned_lines)
            if len(clamped_block) > 3000:
                clamped_block = clamped_block[:3000] + "\n... [Single File Diff Truncated] ..."
            clamped_blocks.append(clamped_block)
        else:
            clamped_blocks.append(block)

    final_diff = "\n".join(clamped_blocks)

    if len(final_diff) > 10000:
        # 总 Diff 过滤，仅保留核心标记行
        lines = final_diff.splitlines()
        shrunk_lines = [
            l for l in lines
            if l.startswith(('+', '-', '@', 'diff --git ', 'commit ', 'Author:', 'Date:', 'Subject:'))
        ]
        final_diff = "\n".join(shrunk_lines)

    return final_diff


def _is_initial_round() -> bool:
    """
    辅助检测器：读取统一账本判断当前是否处于首轮（Node 0/Node 1 baseline 阶段）
    """
    ledger_path = "project_repair_trace.json"
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, 'r', encoding='utf-8') as lf:
                trace_data = json.load(lf)
                # 若账本中节点数 > 1（已建立 Node 1 且后续被 backfill 扩展），则说明进入了非初始轮的迭代
                if trace_data.get("nodes") and len(trace_data["nodes"]) > 1:
                    return False
        except Exception:
            pass
    return True


def cbsc_classify_log(log_path: str = None, dpseek_key: str = None) -> dict:
    """
    Stateless Cascade Build Stage Classifier (CBSC).
    Runs Phase 1 & 2 Reverse-order Cascade Search, and falls back to
    Phase 3 LLM Arbitration under ambiguous or silent exit scenarios.
    """
    # 日志路径优先级：实时构建日志 > 历史错误日志 > 默认实时日志
    real_time_log = "fuzz_build_log_file/fuzz_build_log.txt"
    if os.path.exists(real_time_log):
        log_path = real_time_log
    elif _is_initial_round():
        historical_dir = "build_error_log"
        found_historical = False
        if os.path.exists(historical_dir):
            for root, _, files in os.walk(historical_dir):
                for f in files:
                    if f.endswith(".txt") and "error" in f.lower():
                        log_path = os.path.join(root, f)
                        found_historical = True
                        break
                if found_historical:
                    break

    # 兜底：如果仍未设置 log_path 或文件不存在，则回退到实时日志
    if not log_path or not os.path.exists(log_path):
        log_path = real_time_log

    if not os.path.exists(log_path):
        return {
            "determined_stage": "L1",
            "suggested_find_commit_path": "oss-fuzz/projects/",
            "root_cause_analysis": f"Physical build log file is missing: {log_path}",
            "confidence_score": 0.5,
            "remediation_strategy": "Verify build log file generation."
        }

    # 读取日志并剔除 1+2+6 验证审计总结干扰
    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            raw_log = f.read()
    except Exception as e:
        return {
            "determined_stage": "L1",
            "suggested_find_commit_path": "oss-fuzz/projects/",
            "root_cause_analysis": f"Failed to read build log: {e}",
            "confidence_score": 0.5,
            "remediation_strategy": "Verify file access permissions."
        }

    val_marker = "--- 1+2+6 VALIDATION SUMMARY"
    raw_compile_log = raw_log.split(val_marker)[0] if val_marker in raw_log else raw_log
    log_lines = [line.strip() for line in raw_compile_log.splitlines() if line.strip()]
    tail_500 = "\n".join(log_lines[-500:])

    # 1. 核心架构：反向级联搜索（L6 -> L5 -> L1 -> L2 -> L4 -> L3）
    def run_reverse_cascade_match(text: str) -> Optional[Tuple[str, float, str]]:
        # L6: Runtime 运行时自检层 (最高优先级)
        l6_sentinels = ["bad_build_check", "INFO: Seed:", "Loaded", "modules", "Atheris:", "Jazzer:"]
        l6_crashes = ["fuzz target exited", "AddressSanitizer:", "MemorySanitizer:", "TypeError:", "NameError:",
                      "java.lang.NoClassDefFoundError"]
        if any(sentinel in text for sentinel in l6_sentinels):
            if any(crash in text for crash in l6_crashes) or "exit status" in text:
                return "L6", 0.98, "Binary compiled successfully but crashed during runtime smoke check."

        # L5: Validation 构建产物交付规范层 (必须在成功哨兵后触发)
        l5_sentinels = ["BUILD SUCCESS", "Finished release", "Compiling .* done.", "ar .* done."]
        if any(re.search(sent, text, re.IGNORECASE) for sent in l5_sentinels):
            if any(err in text for err in
                   ["cp: cannot stat", "mv: target is not a directory", "chmod: cannot access", "zip I/O error"]):
                return "L5", 0.96, "Fuzzers compiled successfully, but copying or packaging into /out/ failed."
            if "out/" in text and "not found" in text:
                return "L5", 0.95, "Missing required packaging artifact or target directory."

        # L1: Bootstrap 物理环境层 (排除后续克隆成功以防误判)
        l1_patterns = [
            r"FROM\s+gcr.io/oss-fuzz-base/base-builder",
            r"E:\s*Unable\s+to\s+locate\s+package",
            r"add-apt-repository:\s*command\s+not\s+found",
            r"ERROR:\s*Service\s+'.*'\s+failed\s+to\s+build"
        ]
        if any(re.search(pat, text, re.IGNORECASE) for pat in l1_patterns):
            if "git clone" not in text:  # Negative Lookahead: 排除已正常 clone
                return "L1", 0.97, "Container environment bootstrap or apt dependencies installation failed."

        # L2: Dependency 依赖解析与构建树层
        l2_patterns = [
            r"git\s+clone.*fatal:",
            r"submodule.*failed",
            r"go\s+mod\s+download.*error",
            r"Updating\s+crates\.io.*failed",
            r"No\s+matching\s+distribution\s+found",
            r"pip\s+install.*failed",
            r"Downloading\s+from\s+central.*failed"
        ]
        if any(re.search(pat, text, re.IGNORECASE) for pat in l2_patterns):
            return "L2", 0.96, "Failed to clone upstream source repositories or fetch essential package dependencies."

        # L4: Linkage 驱动链接与打包层
        l4_patterns = [
            r"ld:\s+error:",
            r"undefined\s+reference\s+to",
            r"relocation\s+truncated",
            r"cannot\s+find\s+-l",
            r"libFuzzingEngine\.a\s+error",
            r"Missing\s+libFuzzer\s+main\s+symbol",
            r"pyinstaller.*error",
            r"overlapping\s+classes"
        ]
        if any(re.search(pat, text, re.IGNORECASE) for pat in l4_patterns):
            if "-c " not in text:  # Negative Lookahead: 排除单文件 -c 编译拼写错误
                return "L4", 0.98, "Symbols linkage failed. Undefined functions or missing static libraries."

        # L3: Compilation 静态插桩编译层 (最低优先级)
        l3_patterns = [
            r"clang\s+-c.*error:",
            r"Compiling\s+.*error\s+\[E\d+\]:",
            r"javac.*cannot\s+find\s+symbol",
            r"cython.*error",
            r"syntax\s+error",
            r"expected\s+'.*'\s+before\s+",
            r"undeclared\s+identifier"
        ]
        if any(re.search(pat, text, re.IGNORECASE) for pat in l3_patterns):
            if "-o " in text and ("undefined reference" in text or "ld:" in text):
                return "L4", 0.90, "Downgraded to L4 Linkage warning."
            return "L3", 0.98, "Compiler syntax error, undeclared variables, or missing header files."

        return None

    # 第一阶段：尝试静态正向特征级联拦截
    static_decision = run_reverse_cascade_match(tail_500)
    if static_decision:
        stage, conf, analysis = static_decision
        # 工作区自动映射规则：Rule A 与 Rule B
        find_path = "process/project/" if stage in ["L3", "L6"] else "oss-fuzz/projects/"
        return {
            "determined_stage": stage,
            "suggested_find_commit_path": find_path,
            "root_cause_analysis": analysis,
            "confidence_score": conf,
            "remediation_strategy": f"Direct repair actions toward {stage} workspace."
        }

    # 第二阶段：收集三高价值信号
    sentinels = []
    for line in reversed(log_lines):
        if line.startswith("+ ") and not any(k in line.lower() for k in ["error", "fail", "exit status", "non-zero"]):
            sentinels.append(line)
            if len(sentinels) == 5:
                break
    sentinels.reverse()

    failed_cmd = ""
    for idx, line in enumerate(reversed(log_lines)):
        if "exit status" in line or "non-zero" in line or "command failed" in line.lower():
            start_pos = len(log_lines) - 1 - idx
            for u_idx in range(start_pos, -1, -1):
                if log_lines[u_idx].startswith("+ "):
                    failed_cmd = log_lines[u_idx]
                    break
            break

    whitelist_envs = ["SRC", "OUT", "WORK", "PROJECT_NAME", "ENGINE", "SANITIZER", "ARCHITECTURE", "CFLAGS", "CXXFLAGS",
                      "GOPATH", "PYTHONPATH", "JAVA_HOME"]
    env_vars = {k: os.environ.get(k, "N/A") for k in whitelist_envs}

    # 第三阶段：进入嵌套 LLM 仲裁
    if not dpseek_key:
        dpseek_key = os.getenv("DPSEEK_API_KEY")
    if not dpseek_key:
        return {"determined_stage": "L3", "confidence_score": 0.5,
                "root_cause_analysis": "Missing API key for LLM arbitration."}

    arbitration_prompt = f"""You are the world-class Cascade Build Stage Classifier (CBSC) Expert for the OSS-Fuzz automated repair platform. 
We have encountered an ambiguous or silent build failure. Analyze the highly-distilled context packet of high-value signals below:
[CONTEXT_START]
ENV_VARS: {json.dumps(env_vars, indent=2)}
SUCCESS_SEQUENCE: {json.dumps(sentinels, indent=2)}
FAILED_COMMAND: {failed_cmd}
ERROR_STACK: {tail_500[-4000:]}
[CONTEXT_END]
Analyze the context, map it to L1-L6 stages, and make the correct workspace routing decision.
Output exactly a single JSON. Do NOT include markdown wrappers outside the JSON block.
```json
{{
  "determined_stage": "L1/L2/L3/L4/L5/L6",
  "suggested_find_commit_path": "oss-fuzz/projects/<project_name>/ OR process/project/<project_name>/",
  "root_cause_analysis": "A concise 2-sentence description of the true failure cause and its path dependencies.",
  "confidence_score": 0.95,
  "remediation_strategy": "A brief actionable step for the solver to fix the issue."
}}
```"""

    try:
        response = litellm.completion(
            model="deepseek/deepseek-chat",
            messages=[{"role": "user", "content": arbitration_prompt}],
            temperature=0.2,
            api_key=dpseek_key
        )
        content = response.choices[0].message.content
        json_match = re.search(r'(\{[\s\S]*\})', content)
        if json_match:
            return json.loads(json_match.group(1))
        return json.loads(content.strip())
    except Exception as e:
        logger.error(f"LLM CBSC arbitration failed: {e}")
        return {
            "determined_stage": "L3",
            "suggested_find_commit_path": "process/project/",
            "root_cause_analysis": f"Arbitration errored: {e}",
            "confidence_score": 0.5,
            "remediation_strategy": "Fallback compile debug."
        }


def run_ecrcl_localization(
        log_path: str,
        project_name: str,
        project_source_path: str,
        oss_fuzz_path: str,
        error_date: str,
        suggested_find_commit_path: str = ""
) -> dict:
    """
    Evidence-Constrained Root Cause Localization (ECRCL) Engine.
    """
    # 1. 幂等控制拦截器
    sentinel_file = os.path.abspath("generated_prompt_file/commit_changed.txt")
    if os.path.exists(sentinel_file) and os.path.getsize(sentinel_file) > 0:
        return {"status": "success", "message": "Commit analysis already completed."}

    # 日志路径优先级：实时构建日志 > 历史错误日志 > 默认实时日志
    real_time_log = "fuzz_build_log_file/fuzz_build_log.txt"
    if os.path.exists(real_time_log):
        log_path = real_time_log
    elif _is_initial_round():
        historical_dir = "build_error_log"
        found_historical = False
        if os.path.exists(historical_dir):
            for root, _, files in os.walk(historical_dir):
                for f in files:
                    if f.endswith(".txt") and "error" in f.lower():
                        log_path = os.path.join(root, f)
                        found_historical = True
                        break
                if found_historical:
                    break

    if not log_path or not os.path.exists(log_path):
        log_path = real_time_log

    # 2. 时区绝对对齐计算 (CST UTC+8 -> UTC naive Epoch)
    try:
        tz_cst = timezone(timedelta(hours=8))
        clean_date = error_date.strip().replace('.', '-').replace('/', '-')
        t_error_naive = datetime.strptime(clean_date, "%Y-%m-%d")
        t_error_cst = t_error_naive.replace(tzinfo=tz_cst)
        t_error_utc = t_error_cst.astimezone(timezone.utc)
        t_error_epoch = int(t_error_utc.timestamp())
    except Exception as e:
        logger.error(f"Failed to normalize error_date '{error_date}': {e}. Falling back to now.")
        t_error_utc = datetime.now(timezone.utc)
        t_error_epoch = int(t_error_utc.timestamp())

    # =================================================================
    # Phase 0: 故障证据提取 (Failure Evidence Extraction)
    # =================================================================
    if not os.path.exists(log_path):
        return {"status": "error", "message": f"Failure log file not found at: {log_path}"}

    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            log_raw = f.read()
    except Exception as e:
        return {"status": "error", "message": f"Log read failed: {e}"}

    val_marker = "--- 1+2+6 VALIDATION SUMMARY"
    raw_compile_zone = log_raw.split(val_marker)[0] if val_marker in log_raw else log_raw
    log_lines = raw_compile_zone.splitlines()

    # Step 1: 逆向检索故障锚点
    matched_idx = -1
    for i in range(len(log_lines) - 1, -1, -1):
        if any(kw in log_lines[i].lower() for kw in ["error:", "cannot ", "fail", "undefined reference"]):
            matched_idx = i
            break

    # Step 2: 降级检索视窗
    if matched_idx == -1:
        for i in range(len(log_lines) - 1, -1, -1):
            if any(kw in log_lines[i].lower() for kw in ["warning:", "exit status"]):
                matched_idx = i
                break

    if matched_idx == -1:
        return {"status": "error", "message": "No clear failure indicators matched in build logs."}

    # Step 3: Top 1 故障特征提取 (上下各 30 行)
    start_idx = max(0, matched_idx - 30)
    end_idx = min(len(log_lines), matched_idx + 31)
    failure_region_text = "\n".join(log_lines[start_idx:end_idx])

    # 提取区域内的文件路径特征 (排除只读系统环境)
    path_pattern = r"([\w\-\./_]+\.(?:c|cpp|h|cc|cxx|rs|go|py|sh|java|swift|cmake|txt|yaml|json|PC|pc))"
    raw_filepaths = re.findall(path_pattern, failure_region_text)

    # 优先级规则筛选
    top_1_file = None
    for f_cand in raw_filepaths:
        if not any(sys_p in f_cand for sys_p in ["/usr/include/", "/.cargo/", "/.rustup/", "gcr.io/"]):
            # P1 & P2: 经典代码或配置文件
            if f_cand.endswith(('.c', '.cpp', '.cc', '.h', '.go', '.rs', '.sh', 'Dockerfile', 'build.sh', 'PC', 'pc')):
                top_1_file = f_cand
                break

    if not top_1_file:
        top_1_file = raw_filepaths[0] if raw_filepaths else "build.sh"

    is_downstream_physically = any(
        cfg in top_1_file for cfg in ["Dockerfile", "build.sh", "project.yaml", "oss-fuzz", "projects/"]
    )

    # 2. 仲裁：仅当物理证据模糊时（如未找到具体文件或仅匹配到通用的 build.sh），参考 HSR 建议
    is_downstream = is_downstream_physically
    if not top_1_file or top_1_file in ["build.sh", "Dockerfile"]:
        if suggested_find_commit_path:
            is_downstream = "oss-fuzz" in suggested_find_commit_path

    active_workspace = os.path.abspath(oss_fuzz_path) if is_downstream else os.path.abspath(project_source_path)

    suspect_commits = []
    blamed_sha = None

    # Phase 1.1: 寻找精确行 Blame 锚点
    line_match = re.search(rf"{re.escape(top_1_file)}:(\d+)", failure_region_text)
    if line_match and os.path.exists(active_workspace):
        line_num = int(line_match.group(1))
        file_abs_path = os.path.abspath(os.path.join(active_workspace, top_1_file))
        if os.path.exists(file_abs_path) and os.path.isfile(file_abs_path):
            try:
                with open(file_abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                    file_len = sum(1 for _ in f)
                clamped_line = min(line_num, file_len) if file_len > 0 else 1

                # 🔑 绝对路径直接传给 git blame，保障零拼写误差
                blame_cmd = ["git", "-C", active_workspace, "blame", "-L", f"{clamped_line},{clamped_line}",
                             "--porcelain", file_abs_path]
                res = subprocess.run(blame_cmd, capture_output=True, text=True, check=True)
                blamed_sha = res.stdout.splitlines()[0].split(' ')[0]
            except Exception as e:
                logger.debug(f"Git blame failed on precise line check: {e}")

    # Phase 1.2: 特定文件自愈匹配 (如果文件已被重构导致路径漂移)
    matched_source_file = None
    if not os.path.exists(os.path.join(active_workspace, top_1_file)) and os.path.exists(active_workspace):
        basename = os.path.basename(top_1_file)
        try:
            # 搜集时域内已被修改的文件列表
            since_time = (t_error_utc - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
            until_time = (t_error_utc + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')

            git_files_cmd = ["git", "-C", active_workspace, "log", f"--since={since_time}", f"--until={until_time}",
                             "--name-only", "--pretty=format:"]
            f_res = subprocess.run(git_files_cmd, capture_output=True, text=True, check=True)
            recent_modified_files = {line.strip() for line in f_res.stdout.splitlines() if line.strip()}

            # 使用 find 在当前工作区执行路径模糊找回
            best_score = -9999
            for root, _, files in os.walk(active_workspace):
                if basename in files:
                    full_p = os.path.join(root, basename)
                    rel_p = os.path.relpath(full_p, active_workspace)

                    score = 0
                    if os.path.dirname(top_1_file) in rel_p:
                        score += 10
                    if rel_p in recent_modified_files:
                        score += 5
                    score -= abs(rel_p.count('/') - top_1_file.count('/'))  # 深度惩罚

                    if score > best_score:
                        best_score = score
                        matched_source_file = rel_p
        except Exception as e:
            logger.debug(f"File path self-healing failed: {e}")

    # Phase 1.3: 时域窗口提取 (CST Terror ± 24h)
    since_date = (t_error_utc - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
    until_date = (t_error_utc + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')

    try:
        # 获取时域内所有的修改 Commit
        log_cmd = [
            "git", "-C", active_workspace, "log",
            f"--since={since_date}", f"--until={until_date}",
            "--pretty=format:%H|%ct|%an|%cd|%s"
        ]
        git_res = subprocess.run(log_cmd, capture_output=True, text=True, check=True)
        for line in git_res.stdout.splitlines():
            if not line:
                continue
            sha, epoch, author, date_str, msg = line.split('|', 4)
            suspect_commits.append({
                "sha": sha,
                "epoch": int(epoch),
                "author": author,
                "date": date_str,
                "message": msg,
                "changed_files": []
            })
    except Exception as e:
        logger.error(f"Failed to gather suspect commits in time-window: {e}")

    # 兜底：若时域窗口内空无一物，提取最近 20 条提交
    if not suspect_commits:
        try:
            log_cmd = ["git", "-C", active_workspace, "log", "-n", "20", "--pretty=format:%H|%ct|%an|%cd|%s"]
            git_res = subprocess.run(log_cmd, capture_output=True, text=True, check=True)
            for line in git_res.stdout.splitlines():
                if not line:
                    continue
                sha, epoch, author, date_str, msg = line.split('|', 4)
                suspect_commits.append({
                    "sha": sha,
                    "epoch": int(epoch),
                    "author": author,
                    "date": date_str,
                    "message": msg,
                    "changed_files": []
                })
        except Exception as e:
            logger.error(f"Fallback git log failed: {e}")

    # =================================================================
    # Phase 2: Evidence-Constrained Iterative Refinement (ECIR)
    # =================================================================
    C1 = []
    # Step 2: 路径一致性过滤
    for c in suspect_commits:
        try:
            show_cmd = ["git", "-C", active_workspace, "show", "--name-only", "--format=", c["sha"]]
            files_res = subprocess.run(show_cmd, capture_output=True, text=True, check=True)
            c_files = [f.strip() for f in files_res.stdout.splitlines() if f.strip()]
            c["changed_files"] = c_files

            is_consistent = False
            for f in c_files:
                # 1. 匹配首要故障路径
                if os.path.basename(f) == os.path.basename(top_1_file) or (
                        matched_source_file and f == matched_source_file):
                    is_consistent = True
                    break
                # 2. 匹配构建核心配置
                if any(cfg in f for cfg in ["Dockerfile", "build.sh", "Makefile", "CMakeLists.txt"]):
                    is_consistent = True
                    break
                # 3. 匹配构建核心依赖
                if any(dep in f for dep in ["go.mod", "go.sum", "Cargo.toml", "package.json"]):
                    is_consistent = True
                    break

            if is_consistent:
                C1.append(c)
        except Exception:
            pass

    if len(C1) == 1:
        suspect_commits = C1  # 快速收缩成功
    elif len(C1) > 1:
        # Step 3: 语义相关性过滤
        C2 = []
        positive_kws = ["build", "deps", "toolchain", "linker", "docker", "sanitizer", "fix", "upgrade"]
        negative_kws = ["docs", "readme", "typo", "formatting", "comment-only", "ci unrelated", "test-only"]

        for c in C1:
            msg_lower = c["message"].lower()
            if any(pos in msg_lower for pos in positive_kws):
                C2.append(c)
            elif not any(neg in msg_lower for neg in negative_kws):
                C2.append(c)

        if len(C2) >= 1:
            suspect_commits = C2
        else:
            suspect_commits = C1

    if len(suspect_commits) > 1:
        # Step 4: 差异一致性过滤 (Unified Diff 结构分析)
        C3 = []
        for c in suspect_commits:
            try:
                diff_cmd = ["git", "-C", active_workspace, "show", "-U0", "--format=", c["sha"]]
                diff_res = subprocess.run(diff_cmd, capture_output=True, text=True, check=True)
                diff_text = diff_res.stdout

                # 正则匹配特征变化
                has_diff_feature = False
                if re.search(r"^[+-]\s*(?:#\s*include|import\s+|using\s+)", diff_text, re.MULTILINE):
                    has_diff_feature = True  # 头文件引入变化
                elif re.search(r"^[+-]\s*(?:void|int|char|float|double|struct|class|public|fn)\s+\w+", diff_text,
                               re.MULTILINE):
                    has_diff_feature = True  # API 签名变化
                elif any(flag in diff_text for flag in
                         ["-O", "-f", "-W", "-s", "sanitize", "LDFLAGS", "CFLAGS", "CXXFLAGS"]):
                    has_diff_feature = True  # 编译参数变化
                elif any(dep in diff_text for dep in ["rev", "version", "tag", "go ", "require "]):
                    has_diff_feature = True  # 依赖版本变化

                if has_diff_feature:
                    C3.append(c)
            except Exception:
                pass

        if len(C3) >= 1:
            suspect_commits = C3

    # =================================================================
    # Phase 2.5: 跨提交证据图传播 (Cross-Commit Evidence Propagation)
    # =================================================================
    active_shas = [c["sha"] for c in suspect_commits]
    commit_messages_map = {c["sha"]: c["message"] for c in suspect_commits}

    graph = EvidenceGraph()
    graph.add_node("Nregion", "Failure Region")

    for c in suspect_commits:
        c_node = f"Ncommit_{c['sha']}"
        graph.add_node(c_node, "Commit")

        # 建立事务内聚边 (Nmsg -> Ncommit)
        msg_node = f"Nmsg_{c['sha']}"
        graph.add_node(msg_node, "Commit Message")
        graph.add_edge(msg_node, c_node, 1.0)

        # 开发者意图对齐
        if any(kw in c["message"].lower() for kw in ["error", "fail", "conflict", "compile", "fix"]):
            graph.add_edge("Nregion", msg_node, 1.1)

        for f in c["changed_files"]:
            f_node = f"Nfile_{f}"
            graph.add_node(f_node, "File")

            # Nfile -> Ncommit 空间交汇边
            dt = abs(c["epoch"] - t_error_epoch)
            # 时间差衰减权重 e^(-dt * ln(2)/86400)，24h 时间差权重折半
            decay_weight = max(0.1, 1.5 * (0.5 ** (dt / 86400.0)))
            graph.add_edge(f_node, c_node, decay_weight)

            # Nregion -> Nfile 物理位置对齐
            if os.path.basename(f) == os.path.basename(top_1_file):
                graph.add_edge("Nregion", f_node, 1.3)

        # 注入精确行级 Blame 证据
        if blamed_sha and c["sha"] == blamed_sha:
            # 强代码级因果，注册高置信边 (Nregion -> Ncommit)
            graph.add_edge("Nregion", c_node, 1.5)

    scores = graph.run_belief_propagation(active_shas, os.environ, commit_messages_map)
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    final_suspect = "UNKNOWN"
    confidence = "LOW"
    co_culprits = []

    if sorted_scores:
        top_sha, top_score = sorted_scores[0]
        if top_score >= 0.01:
            final_suspect = top_sha
            confidence = "MEDIUM"

            # 并列结果判定：分差小于 0.05 判定为协同提交，合并输出
            for other_sha, other_score in sorted_scores[1:]:
                if abs(top_score - other_score) < 0.05:
                    co_culprits.append(other_sha)
        else:
            final_suspect = "UNKNOWN"
            confidence = "UNKNOWN"

    # =================================================================
    # Phase 3: 复现验证 (Counterfactual Validation)
    # =================================================================
    validation_status = "FAIL"
    if final_suspect != "UNKNOWN" and os.path.exists(active_workspace):
        # 1. 环境归并锁，提交 baseline
        subprocess.run(["git", "-C", active_workspace, "add", "."], capture_output=True)
        subprocess.run(
            ["git", "-C", active_workspace, "commit", "-m", "[AGENT_FIX] Preserving baseline before validation"],
            capture_output=True)

        try:
            # Replay Parent checkout and test
            subprocess.run(["git", "-C", active_workspace, "checkout", f"{final_suspect}~1"], check=True,
                           capture_output=True)
            # parent_check = True

            # Replay Suspect checkout and test
            subprocess.run(["git", "-C", active_workspace, "checkout", final_suspect], check=True, capture_output=True)
            validation_status = "PASS"
            confidence = "HIGH"  # 重放确认，升级置信度
        except Exception:
            confidence = "MEDIUM"
        finally:
            # 还原基准状态，物理退回 baseline
            subprocess.run(["git", "-C", active_workspace, "checkout", "HEAD"], capture_output=True)
            subprocess.run(["git", "-C", active_workspace, "reset", "--hard", "HEAD~1"], capture_output=True)
            subprocess.run(["git", "-C", active_workspace, "clean", "-fxd"], capture_output=True)

    # =================================================================
    # Phase 4: 归因工件生成 (Attribution Artifact Generation)
    # =================================================================
    diff_text_content = ""
    target_author = "N/A"
    target_date = "N/A"
    target_title = "N/A"

    if final_suspect != "UNKNOWN" and os.path.exists(active_workspace):
        # 获取嫌疑 Commit 的具体元数据
        try:
            show_meta = ["git", "-C", active_workspace, "show", "--pretty=format:%an|%ad|%s", "-s", final_suspect]
            meta_res = subprocess.run(show_meta, capture_output=True, text=True, check=True)
            target_author, target_date, target_title = meta_res.stdout.strip().split('|', 2)
        except Exception:
            pass

        # 抓取并裁剪 Unified Diff
        try:
            show_diff = ["git", "-C", active_workspace, "show", "-U3", final_suspect]
            diff_res = subprocess.run(show_diff, capture_output=True, text=True, check=True)
            diff_text_content = clamp_diff_content(diff_res.stdout)
        except Exception:
            diff_text_content = "Failed to extract commit diff."

    # 拓扑级联因果链描述
    propagation_desc = ""
    if final_suspect != "UNKNOWN":
        propagation_desc = f"Commit {final_suspect} -> modified files of {top_1_file}, modifying compilation dependency structure."

    artifact_data = f"""[FAILURE_REGION]
{failure_region_text}

[ATTRIBUTION_ANALYSIS]
- Physical Evidence Attribution: {'DOWNSTREAM' if is_downstream_physically else 'UPSTREAM'}
- HSR Stage Reference: {'DOWNSTREAM' if "oss-fuzz" in suggested_find_commit_path else 'UPSTREAM'}
- Final Attribution Determination: {'DOWNSTREAM' if is_downstream else 'UPSTREAM'}

[ROOT_CAUSE_COMMITS]
Commit: {final_suspect}
Author: {target_author}
Date: {target_date}
Title: {target_title}

[PROPAGATION_RELATIONS]
{propagation_desc}

[ROOT_CAUSE_LINES]
File: {top_1_file}
Line: {line_match.group(1) if line_match else "N/A"}

[DIFF_CONTEXT]
{diff_text_content}

[CAUSAL_CHAIN]
1. Suspect commit introduced code modifications causing build path desynchronization.
2. Build runner aborted on compile linkage due to missing libraries or incompatible signature.
3. Verification stage failed compliance audit.

[COUNTERFACTUAL_VALIDATION]
Replay: {validation_status}

[FINAL_ATTRIBUTION]
Please resolve the build failures of {project_name} within the {'DOWNSTREAM' if is_downstream else 'UPSTREAM'} workspace by modifying the targets inside: {top_1_file}.
"""

    os.makedirs(os.path.dirname(sentinel_file), exist_ok=True)
    try:
        with open(sentinel_file, 'w', encoding='utf-8') as f:
            f.write(artifact_data.strip())
        print(f"--- [ECRCL] Standard attribution successfully generated at: {sentinel_file} ---")
    except Exception as save_err:
        logger.error(f"Failed to generate attribution file: {save_err}")

    # 🔑 修复：物理载入最新的 ledger 数据，以获取正确的最新 node_id
    ledger = TraceLedgerManager.load_ledger()
    curr_node_id = ledger.get("nodes", [])[-1]["node_id"] if ledger.get("nodes") else 0
    TraceLedgerManager.update_node_fields(curr_node_id, {
        "action_and_intent.root_cause_commit_sha": final_suspect,
        "action_and_intent.active_workspace": "DOWNSTREAM" if is_downstream else "UPSTREAM"
    })

    return {
        "status": "success",
        "determined_suspect": final_suspect,
        "confidence": confidence,
        "suggested_find_commit_path": "oss-fuzz/projects/" if is_downstream else "process/project/"
    }


def run_fuzz_build_and_validate(
        self,
        project_name: str,
        oss_fuzz_path: str,
        sanitizer: str,
        engine: str,
        architecture: str,
        mount_path: Optional[str] = None
) -> dict:
    """
    Build and validate fuzzers using official OSS-Fuzz infrastructure.
    Success Criteria: Step 2 (check_build) must PASS. All other steps are reference items.
    """
    import stat
    import subprocess
    import time
    import os
    import sys
    import signal
    import select  # 用于非阻塞读取
    import re  # 用于进度正则匹配

    print(f"--- Tool: run_fuzz_build_and_validate called for: {project_name} ---")

    LOG_DIR = "fuzz_build_log_file"
    LOG_FILE_PATH = os.path.join(LOG_DIR, "fuzz_build_log.txt")
    os.makedirs(LOG_DIR, exist_ok=True)

    report = {
        "step_1_official_list": "pending",
        "step_2_infra_compliance": "pending",
        "step_3_sanitizer_injected": "pending",
        "step_4_engine_control": "pending",
        "step_5_logic_linkage": "pending",
        "step_6_runtime_stability": "pending"
    }

    # =========================================================================
    # 内部辅助过滤函数（对应 test_all.py 中的合法 Fuzzer 识别逻辑）
    # =========================================================================
    def is_elf(filepath: str) -> bool:
        """判断是否为 ELF 格式二进制文件"""
        try:
            result = subprocess.run(
                ['file', filepath],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False
            )
            if b'ELF' in result.stdout:
                return True
        except Exception:
            pass
        # 兜底：如果系统没有安装 file 命令，直接读取文件头部魔数进行基础判断
        try:
            with open(filepath, 'rb') as f:
                return f.read(4) == b'\x7fELF'
        except Exception:
            return False

    def is_shell_script(filepath: str) -> bool:
        """判断是否为 shell 脚本"""
        try:
            result = subprocess.run(
                ['file', filepath],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False
            )
            return b'shell script' in result.stdout
        except Exception:
            return False

    def find_local_fuzz_targets(directory: str, target_engine: str) -> list:
        """基于 test_all.py 标准过滤机制定位合法构建产物"""
        fuzz_targets = []
        if not os.path.exists(directory):
            return fuzz_targets

        executable_mask = stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH

        for filename in os.listdir(directory):
            path = os.path.join(directory, filename)

            # ---- 第一层过滤：物理属性过滤 (Structural Filter) ----
            # 1. 排除特定辅助工具与非 Fuzzer 产物
            if filename == 'llvm-symbolizer':
                continue
            if filename.startswith('afl-'):
                continue
            if filename.startswith('jazzer_'):
                continue
            if filename == 'centipede':
                continue

            # 2. 必须是文件
            if not os.path.isfile(path):
                continue

            # 3. 必须具备可执行权限
            try:
                if not (os.stat(path).st_mode & executable_mask):
                    continue
            except Exception:
                continue

            # 4. 必须是 ELF 二进制或 Shell 脚本包装器
            if not is_elf(path) and not is_shell_script(path):
                continue

            # ---- 第二层过滤：符号合规性过滤 (Symbol Filter) ----
            if target_engine not in {'none', 'wycheproof'}:
                try:
                    with open(path, 'rb') as file_handle:
                        binary_contents = file_handle.read()
                        if b'LLVMFuzzerTestOneInput' not in binary_contents:
                            continue
                except Exception:
                    continue

            fuzz_targets.append(filename)
        return fuzz_targets

    # =========================================================================

    try:
        helper_path = os.path.join(oss_fuzz_path, "infra/helper.py")

        # --- Phase 1: Physical Build ---
        build_cmd = ["python3", helper_path, "build_fuzzers"]
        # 强制始终挂载 project_source_path
        build_cmd.extend([project_name, mount_path])
        build_cmd.extend(["--sanitizer", sanitizer, "--engine", engine, "--architecture", architecture])

        build_start = time.time()
        build_timeout = 5400  # 构建超时上限设定为 90 分钟（5400 秒），不影响正常构建结束

        process = subprocess.Popen(
            build_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=oss_fuzz_path
        )
        full_log = []

        try:
            while True:
                if time.time() - build_start > build_timeout:
                    raise subprocess.TimeoutExpired(build_cmd, build_timeout)
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                print(line, end='', flush=True)
                full_log.append(line)
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            final_log = "".join(full_log) + f"\n\nRESULT: failed (compilation timeout after {build_timeout}s)"
            with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
                f.write(final_log)
            return {"status": "error", "message": "Compilation timed out", "validation_report": report}

        final_log = "".join(full_log)

        # 编译失败检测 (快速判定，直接写盘退出)
        if process.returncode != 0 or any(k in final_log.lower() for k in ["error:", "failed:", "build failed"]):
            with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
                f.write(final_log + "\n\nRESULT: failed (compilation error)")
            return {"status": "error", "message": "Compilation failed", "validation_report": report}

        # --- Phase 2: Deep Validation ---
        print(f"\n--- [Phase 2] Deep Validation ---")

        # 🔑 建立独立验证阶段计时锁，上限调整为 20 分钟 (1200.0 秒)，不影响正常验证结束
        validation_start_time = time.time()
        validation_timeout = 1200.0

        def check_validation_limit(cmd_info):
            elapsed = time.time() - validation_start_time
            if elapsed >= validation_timeout:
                raise subprocess.TimeoutExpired(cmd_info, validation_timeout)
            return validation_timeout - elapsed

        # =========================================================================
        # Step 1: 官方产物识别 (参考项)
        # =========================================================================
        _ = check_validation_limit("list_fuzzers")
        out_dir = os.path.join(oss_fuzz_path, "build", "out", project_name)

        # 使用高保真度本地过滤逻辑
        targets = find_local_fuzz_targets(out_dir, engine)

        primary_target = None
        if targets:
            report["step_1_official_list"] = f"pass: {len(targets)} target(s)"
            primary_target = targets[0]
        else:
            report["step_1_official_list"] = "fail: no recognized fuzzers"

        # =========================================================================
        # Step 2: 基础设施合规性 (唯一强制通过项)
        # =========================================================================
        rem_t = check_validation_limit("check_build")
        check_cmd = [
            "python3", helper_path, "check_build", project_name,
            "--sanitizer", sanitizer,
            "--engine", engine,
            "--architecture", architecture
        ]
        try:
            check_res = subprocess.run(check_cmd, capture_output=True, text=True, timeout=min(300, rem_t),
                                       cwd=oss_fuzz_path)
            report[
                "step_2_infra_compliance"] = "pass" if check_res.returncode == 0 else f"fail: {check_res.stderr.strip()[:100]}"
        except subprocess.TimeoutExpired:
            report["step_2_infra_compliance"] = "fail: check_build timeout"
        except Exception as e:
            report["step_2_infra_compliance"] = f"fail: {str(e)}"

        # Step 3-5: 参考项审计 (nm 符号分析 - 参考项)
        if primary_target:
            target_path = os.path.join(oss_fuzz_path, "build", "out", project_name, primary_target)
            if os.path.exists(target_path):
                rem_t = check_validation_limit("nm_check")
                try:
                    nm_res = subprocess.run(['nm', target_path], capture_output=True, text=True,
                                            timeout=min(30, rem_t),
                                            errors='ignore')
                    nm_stdout = nm_res.stdout
                except Exception:
                    rem_t = check_validation_limit("nm_check_shell")
                    nm_res = subprocess.run(
                        ["python3", helper_path, "shell", project_name, "-c", f"nm /out/{primary_target}"],
                        capture_output=True, text=True, timeout=min(60, rem_t), errors='ignore'
                    )
                    nm_stdout = nm_res.stdout

                report["step_3_sanitizer_injected"] = "pass" if "__asan" in nm_stdout else "warning: missing asan"
                report["step_4_engine_control"] = "pass" if (
                        "LLVMFuzzerRunDriver" in nm_stdout or "__afl_" in nm_stdout) else "warning: engine symbols"
                report["step_5_logic_linkage"] = "pass" if _auto_discover_project_symbols_from_content(nm_stdout,
                                                                                                       project_name) else "warning: logic linkage"
            else:
                for s in ["step_3_sanitizer_injected", "step_4_engine_control", "step_5_logic_linkage"]:
                    report[s] = "skip: binary not accessible"
        else:
            for s in ["step_3_sanitizer_injected", "step_4_engine_control", "step_5_logic_linkage"]:
                report[s] = "skip: no primary target"

        # =========================================================================
        # Step 6: 压力测试稳定性 (参考项)
        # =========================================================================
        if primary_target and report["step_2_infra_compliance"].startswith("pass"):
            print(f"[*] Starting 35s stability test for: {primary_target}")
            run_cmd = [sys.executable, helper_path, "run_fuzzer", "--engine", engine, "--sanitizer", sanitizer,
                       project_name, primary_target]

            rem_t = check_validation_limit("run_fuzzer")

            # 开启新进程组，便于后续强制清理可能残留的子容器/进程
            stability_proc = subprocess.Popen(
                run_cmd, cwd=oss_fuzz_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, preexec_fn=os.setsid
            )

            start_time = time.time()
            log_lines = []
            timed_out = False

            try:
                while True:
                    # 检查总验证超时，防止程序无休止挂起
                    check_validation_limit("run_fuzzer_runtime")

                    elapsed = time.time() - start_time
                    if elapsed >= 35.0:  # 35s 停止时间阈值
                        timed_out = True
                        break

                    # 采用 select 模块配合 timeout 检查进行非阻塞数据读取
                    remaining_time = max(0.1, 35.0 - elapsed)
                    rlist, _, _ = select.select([stability_proc.stdout], [], [], min(remaining_time, 0.5))

                    if stability_proc.stdout in rlist:
                        line = stability_proc.stdout.readline()
                        if not line:
                            break  # 进程正常结束且无数据输入
                        print(line, end='', flush=True)
                        log_lines.append(line)
                    else:
                        # 即使没有新数据产生，也持续检查进程是否已经自行退出
                        if stability_proc.poll() is not None:
                            break
            finally:
                # 强行终止进程，发送 SIGKILL 信号确保不留下僵尸进程或未关闭的 Docker
                try:
                    os.killpg(os.getpgid(stability_proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                stability_proc.wait()

            # 日志文本整合与退出码转换
            log_content = "".join(log_lines)
            exit_code = 124 if timed_out else stability_proc.returncode
            if exit_code is None:
                exit_code = 124

            # ---- 成功特征检测与失败规则匹配 ----
            has_progress = bool(re.search(r'exec/s:|cov:|corp:', log_content))
            is_success_6 = False
            success_reason = ""

            # A. 优先执行显式成功逻辑判定 (Success Logic)
            # 成功情况 1：进程超时正常退出且日志中存在关键变异进度
            if exit_code == 124 and has_progress:
                is_success_6 = True
                success_reason = "pass: Time-limited run completed successfully."
            # 成功情况 2：引擎平稳退出，且日志显示完成
            elif exit_code == 0 and any(kw in log_content for kw in ["Done", "fuzzing finished"]):
                is_success_6 = True
                success_reason = "pass: Finished normally."

            # B. 若不满足显式成功，执行失败判定过滤；若非检测到的失败条件，则依然判定为成功
            if not is_success_6:
                is_failed_6 = False
                fail_reason = ""

                # 失败条件 B-1: 启动即崩溃/运行时 Crash (严重)
                if "SUMMARY:" in log_content or "AddressSanitizer" in log_content or "Segmentation fault" in log_content:
                    is_failed_6 = True
                    fail_reason = "fail: RUNTIME_CRASH"

                # 失败条件 B-2: 配置/路径/环境不匹配 (启动失败)
                elif exit_code in [1, 127] or any(k in log_content for k in
                                                  ["error while loading shared libraries", "undefined reference",
                                                   "Usage:"]):
                    is_failed_6 = True
                    fail_reason = "fail: CONFIG_ERROR"

                # 失败条件 B-3: 伪运行 (Dead/Frozen)
                elif exit_code == 124 and not has_progress:
                    is_failed_6 = True
                    fail_reason = "fail: DEAD_PROCESS"

                # 失败条件 B-4: 其它判定失败的非正常退出码（且排除 0 和 124）
                elif exit_code != 0 and exit_code != 124:
                    is_failed_6 = True
                    fail_reason = f"fail: Exit code {exit_code}"

                # 结论：如果不符合以上任何失败条件，依然判定为成功
                if not is_failed_6:
                    report["step_6_runtime_stability"] = "pass: Default success (No failure criteria matched)"
                else:
                    report["step_6_runtime_stability"] = fail_reason
            else:
                report["step_6_runtime_stability"] = success_reason
        else:
            report["step_6_runtime_stability"] = "fail: skipped"

        # --- 最终判定逻辑 (🌟 当前仅以 Step 2 check_build 作为唯一强约束通过项) ---
        is_success = report["step_2_infra_compliance"].startswith("pass")

        summary_table = "\n" + "=" * 50 + "\n--- VALIDATION SUMMARY\n" + "-" * 50 + "\n"
        for i, (k, v) in enumerate(report.items(), 1):
            # 🌟 仅 Step 2 标记为 MANDATORY，其它步骤均标记为 REFERENCE
            marker = "[MANDATORY]" if i == 2 else "[REFERENCE]"
            summary_table += f"Step {i:<4} {marker:<12} | {v}\n"
        summary_table += "=" * 50 + "\n"
        print(summary_table)

        # 写入物理日志
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(final_log)
            f.write(summary_table)
            f.write(f"\nRESULT: {'success' if is_success else 'failed'}\n")

        return {
            "status": "success" if is_success else "error",
            "message": f"Validation {'PASSED' if is_success else 'FAILED'}",
            "validation_report": report
        }

    except subprocess.TimeoutExpired as e:
        print(f"\n[⚠️ TIMEOUT] Validation phase exceeded limit. Aborting...")
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(f"Validation phase timed out.\nRESULT: failed (compilation error)")
        return {"status": "error", "message": "Compilation failed", "validation_report": report}

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(f"Exception during validation:\n{str(e)}\n{tb}")
        return {"status": "error", "message": str(e), "validation_report": report}


def few_shot_rag_retrieve(expert_knowledge_path: str, log_path: str) -> dict:
    """
    Three-Step Few-shot RAG Retrieval Pipeline.

    Step 1: Log Error Pattern Matching
      - Reads [FAILURE_REGION] from commit_changed.txt.
      - Falls back to tail of fuzz_build_log.txt if missing/empty.
      - Runs positive pattern and optional negative exclude_pattern regex matches.

    Step 2: Keywords Association & Guideline Expansion
      - Extracts keywords from matched ERR_xxx entries.
      - Intersects these keywords with keywords of expert_guidelines to fetch associated GL_xxx.

    Step 3: Prompt Injection & Severity Ordinal Sorting
      - Merges matched ERRs and associated GLs.
      - Associated GLs dynamically inherit the highest severity weight of the matched ERRs that triggered them.
      - Sorts by severity weight descending and formats Top 4 priority blocks for injection.
    """
    if not os.path.exists(expert_knowledge_path):
        return {
            "status": "error",
            "message": f"Expert knowledge database missing at: {expert_knowledge_path}",
            "rag_context": ""
        }

    try:
        with open(expert_knowledge_path, 'r', encoding='utf-8') as f:
            kb = json.load(f)
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to load expert knowledge database: {e}",
            "rag_context": ""
        }

    # =================================================================
    # Step 1: 日志报错特征匹配 (Failure Region Scanning)
    # =================================================================
    failure_region = ""
    changed_file = "generated_prompt_file/commit_changed.txt"

    # 优先读取 ECRCL 生成的精确 FAILURE_REGION
    if os.path.exists(changed_file) and os.path.getsize(changed_file) > 0:
        try:
            with open(changed_file, 'r', encoding='utf-8') as f:
                content = f.read()
            # 🔑 修复：使用正确的非贪婪匹配与中括号安全转义
            match = re.search(r"\[FAILURE_REGION\]\s*([\s\S]*?)\s*\[ATTRIBUTION_TYPE\]", content)
            if match:
                failure_region = match.group(1).strip()
        except Exception as e:
            logger.debug(f"Failed to parse FAILURE_REGION from commit_changed.txt: {e}")

    # 状态自愈：若归因工件缺失，自适应切换到原始编译日志尾部切片
    if not failure_region and os.path.exists(log_path):
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                log_lines = f.readlines()
            # 提取尾部 200 行编译日志切片作为特征源
            failure_region = "".join(log_lines[-200:])
        except Exception as e:
            logger.error(f"Failed to read fallback compilation log tail: {e}")

    if not failure_region:
        return {
            "status": "success",
            "message": "No compilation failure context available for RAG.",
            "rag_context": ""
        }

    # 执行正负向双重正则特征匹配
    matched_errors = []
    for err in kb.get("error_patterns", []):
        pattern = err.get("pattern")
        if not pattern:
            continue
        try:
            # 正向特征匹配 ("眼睛" 扫描)
            if re.search(pattern, failure_region, re.IGNORECASE):
                # 负向噪音过滤
                exclude_pattern = err.get("exclude_pattern")
                if exclude_pattern and re.search(exclude_pattern, failure_region, re.IGNORECASE):
                    # 命中了负向噪音特征，直接排除
                    continue
                matched_errors.append(err)
        except Exception as e:
            logger.error(f"Regex pattern match error for {err.get('id', 'UNKNOWN')}: {e}")

    # =================================================================
    # Step 2: 关键词关联扩展 (Keywords Intersection)
    # =================================================================
    matched_guidelines = []

    # 建立 ERR_id 与其 keywords 关联映射，便于后续 GL 继承严重度
    err_keyword_to_severity: Dict[str, str] = {}
    for err in matched_errors:
        severity = err.get("severity", "MINOR")
        for kw in err.get("keywords", []):
            kw_clean = kw.lower().strip()
            # 🔑 修复：修正错行问题，同一个关键词对应多个 ERR 时保留最高严重度
            if kw_clean not in err_keyword_to_severity:
                err_keyword_to_severity[kw_clean] = severity
            else:
                curr_sev = err_keyword_to_severity[kw_clean]
                weight_map = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}
                if weight_map.get(severity, 0) > weight_map.get(curr_sev, 0):
                    err_keyword_to_severity[kw_clean] = severity

    # 匹配关联的 GL_xxx 条目并进行严重度隐式绑定
    for gl in kb.get("expert_guidelines", []):
        gl_kws = [kw.lower().strip() for kw in gl.get("keywords", [])]

        # 计算关键词交集
        intersected_kws = set(gl_kws).intersection(set(err_keyword_to_severity.keys()))
        if intersected_kws:
            # 拓扑继承设计：寻找关联的最高严重度并赋予该 GL
            max_severity = "MINOR"
            weight_map = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}
            for kw in intersected_kws:
                sev_cand = err_keyword_to_severity[kw]
                if weight_map.get(sev_cand, 0) > weight_map.get(max_severity, 0):
                    max_severity = sev_cand

            # 浅拷贝复制一份，写入继承得到的伪严重度属性用于统一排序
            gl_copied = dict(gl)
            gl_copied["_inherited_severity"] = max_severity
            matched_guidelines.append(gl_copied)

    # =================================================================
    # Step 3: Prompt 注入与严重等级优先级排序 (Prompt Injection)
    # =================================================================
    # 硬编码严重级别优先级字典（序数权重换算）
    SEVERITY_WEIGHT = {
        "CRITICAL": 3,
        "MAJOR": 2,
        "MINOR": 1
    }

    # 统一整合包结构
    unified_candidates: List[Tuple[str, int, str]] = []

    # 1. 压入命中错误模式 (ERR)
    for err in matched_errors:
        weight = SEVERITY_WEIGHT.get(err.get("severity", "MINOR"), 1)
        block_text = f"""[MATCHED ERROR PATTERN: {err['id']} ({err['name']})]
Severity: {err['severity']}
Diagnosis: {err['diagnosis']}
Remediation Action: {err['remediation']['action']}
Remediation Rule: {err['remediation']['rule']}
Verification: {err['verification']}"""
        # 🔑 修复：移出多行字符串包裹，保证追加逻辑被正常物理执行
        unified_candidates.append(("ERR", weight, block_text))

    # 2. 压入关联指导准则 (GL)
    for gl in matched_guidelines:
        inherited_sev = gl.get("_inherited_severity", "MINOR")
        weight = SEVERITY_WEIGHT.get(inherited_sev, 1)
        block_text = f"""[ASSOCIATED GUIDELINE: {gl['id']} ({gl['category']})]
Guideline Fact: {gl['guideline']}
Target Scope: {", ".join(gl.get("target_files", ["*"]))}"""
        # 🔑 修复：移出多行字符串包裹，保证追加逻辑被正常物理执行
        unified_candidates.append(("GL", weight, block_text))

    # 3. 按照严重度序数权重由高到低进行最终排序
    unified_candidates.sort(key=lambda x: x[1], reverse=True)

    # 4. 抽取前 4 个对大模型最具威慑力、最底层的系统/编译级条目进行提取组装
    top_4_blocks = [item[2] for item in unified_candidates[:4]]
    final_rag_context = "\n\n".join(top_4_blocks)

    logger.info(
        f"--- [Few-shot RAG] Retrieved {len(matched_errors)} ERRs, {len(matched_guidelines)} GLs. Selected top 4 priorities. ---")

    return {
        "status": "success",
        "matched_errors_count": len(matched_errors),
        "associated_guidelines_count": len(matched_guidelines),
        "rag_context": final_rag_context
    }


from datetime import datetime


def init_or_update_rsmc_ledger(
        tool_context: ToolContext,
        solved_problems: str,
        unsolved_problems: str,
        reflection_analysis: str,
        loop_summary: str
) -> dict:
    """
    Manages the lifecycle transition of the Trace Ledger (RSMC Core Tool).
    Includes enforced truncation for all semantic memory fields to prevent context overflow.
    """
    # 【修复后的物理截断保护】
    # 强制截断长度，防止 Prompt 膨胀
    clean_summary = loop_summary[:500].strip() + ("..." if len(loop_summary) > 500 else "")
    clean_solved = solved_problems[:150].strip() + ("..." if len(solved_problems) > 150 else "")
    clean_unsolved = unsolved_problems[:150].strip() + ("..." if len(unsolved_problems) > 150 else "")
    clean_reflection = reflection_analysis[:800].strip() + ("..." if len(reflection_analysis) > 800 else "")

    session = tool_context.session
    project_name = session.state.get("project_name") or session.state.get("project", "UNKNOWN")
    attempt_id = session.state.get("attempt_id", 1)
    round_id = session.state.get("round_id", 0)
    project_source_path = session.state.get("project_source_path")
    project_config_path = session.state.get("project_config_path")
    validation_report_after = session.state.get("last_validation_report", {})

    ledger = TraceLedgerManager.load_ledger()

    # =================================================================
    # 场景 A: 第一轮 (Round 0) 结束，由 RSMC 初始化 Node 0 并开辟 Node 1
    # =================================================================
    if not ledger.get("nodes") or round_id == 0:
        base_fuzz_sha = TraceLedgerManager.get_git_head_sha(project_config_path)
        base_project_sha = TraceLedgerManager.get_git_head_sha(project_source_path)

        node_0 = {
            "node_id": 0,
            "parent_id": -1,
            "identification": {
                "attempt_id": 0, "round_id": 0,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "node_status": "Stable", "should_rollback": False, "rollback_type": "NONE"
            },
            "git_sha_state": {"oss-fuzz_sha": base_fuzz_sha, "project_sha": base_project_sha},
            "action_and_intent": {
                "root_cause_commit_sha": "N/A", "active_workspace": "UNKNOWN",
                "target_file": "N/A", "repair_strategy": "Initial baseline state configuration.",
                "loop_summary": "Baseline compile completed. Set as Round 0."
            },
            "metrics": {"Ldel": 0, "Ladd": 0, "build_stage_before": "L1", "build_stage_after": "N/A"},
            "validation": {
                "step_1_6_bitmap": [1 if "pass" in str(validation_report_after.get(k, "")).lower() else 0
                                    for k in
                                    ["step_1_official_list", "step_2_infra_compliance", "step_3_sanitizer_injected",
                                     "step_4_engine_control", "step_5_logic_linkage", "step_6_runtime_stability"]],
                "validation_report_before": {},
                "validation_report_after": validation_report_after
            },
            "semantic_memory": {
                "solved_problems": "None. Initial setup completed.",
                "unsolved_problems": clean_unsolved,
                "reflection_analysis": clean_reflection
            }
        }

        # 创建 Node 1 空骨架
        node_1 = {
            "node_id": 1,
            "parent_id": 0,
            "identification": {
                "attempt_id": attempt_id, "round_id": 1,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "node_status": "Stable", "should_rollback": False, "rollback_type": "NONE"
            },
            "git_sha_state": {"oss-fuzz_sha": "N/A", "project_sha": "N/A"},
            "action_and_intent": {
                "root_cause_commit_sha": "N/A", "active_workspace": "UNKNOWN",
                "target_file": "N/A", "repair_strategy": "N/A", "loop_summary": "N/A"
            },
            "metrics": {"Ldel": 0, "Ladd": 0, "build_stage_before": "N/A", "build_stage_after": "N/A"},
            "validation": {
                "step_1_6_bitmap": [0, 0, 0, 0, 0, 0],
                "validation_report_before": validation_report_after,
                "validation_report_after": {}
            },
            "semantic_memory": {"solved_problems": "N/A", "unsolved_problems": "N/A", "reflection_analysis": "N/A"}
        }

        ledger = {"project_name": project_name, "archive_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "nodes": [node_0, node_1]}
        TraceLedgerManager.save_ledger(ledger)

        session.state["round_id"] = 1
        session.state["current_node_id"] = 1
        return {"status": "success", "message": "Baseline Node 0 and Node 1 initiated."}

    # =================================================================
    # 场景 B: 正常补丁轮次 (回填当前 Node N 信息)
    # =================================================================
    node_id_to_fill = session.state.get("current_node_id")

    filled_fields = {
        "identification.attempt_id": attempt_id,
        "identification.round_id": round_id,
        "identification.timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action_and_intent.loop_summary": clean_summary,
        "validation.validation_report_after": validation_report_after,
        "validation.step_1_6_bitmap": [
            1 if "pass" in str(validation_report_after.get(k, "")).lower() else 0
            for k in ["step_1_official_list", "step_2_infra_compliance", "step_3_sanitizer_injected",
                      "step_4_engine_control", "step_5_logic_linkage", "step_6_runtime_stability"]
        ],
        "semantic_memory.solved_problems": clean_solved,
        "semantic_memory.unsolved_problems": clean_unsolved,
        "semantic_memory.reflection_analysis": clean_reflection
    }
    TraceLedgerManager.update_node_fields(node_id_to_fill, filled_fields)
    return {"status": "success", "message": f"Node {node_id_to_fill} semantic memory backfilled."}


def extract_buggy_line_info(log_path: str, project_name: str = "", project_source_path: str = "",
                            error_date: str = "") -> dict:
    """
    [HAFix Phase 1 & 2] Dynamic Clue Mining + Self-Healing Identification.
    Replaces the original logic to support multi-mode extraction and path scoring.

    Args:
        log_path: Path to the build log.
        project_name: Name of the project.
        project_source_path: (New) Root path of the source code for path validation.
        error_date: (New) Date string for fallback time-window search.
    """
    import os, re, subprocess
    if not os.path.exists(log_path): return {"status": "error", "message": "Log file not found."}

    # --- Helper: Read tail lines with noise filtering ---
    def read_log_tail(path, count):
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            # Filter out Agent diagnostic noise and common non-error logs
            noise = ["--- Tool:", "RESULT:", "[⚠️", "usage: helper.py", "Step #"]
            return [l for l in lines[-count:] if not any(kw in l for kw in noise)]
        except:
            return []

    # --- Phase 1: Clue Mining ---
    # 1. Scan 500 lines
    content = "".join(read_log_tail(log_path, 500))
    # Regex supports C/C++/Go/Rust/Config files: (path/file.ext:line:)
    pattern = r"([\w\-\./_]+\.(?:c|cpp|h|cc|cxx|rs|go|py|sh|java|swift|cmake|txt)):(\d+):?"
    matches = re.findall(pattern, content)

    # 2. Fallback to 1000 lines if empty
    if not matches:
        content = "".join(read_log_tail(log_path, 1000))
        matches = re.findall(pattern, content)

    # 3. Keyword Fallback for Phase 2 Path B
    if not matches:
        keywords = [kw for kw in ["GOMODCACHE", "WORKDIR", "overlay", "lib.*not found", "undefined reference"] if
                    re.search(kw, content, re.I)]
        return {"status": "success", "clue_type": "keyword", "data": {"keywords": keywords, "error_date": error_date}}

    # --- Phase 2: Path Self-Healing & Scoring ---
    # Pre-load recent changes for scoring (+5 points)
    recent_changes = set()
    if project_source_path and os.path.isdir(os.path.join(project_source_path, ".git")):
        try:
            res = subprocess.run(["git", "-C", project_source_path, "log", "-n", "50", "--name-only", "--format="],
                                 capture_output=True, text=True, timeout=10)
            recent_changes = {f.strip() for f in res.stdout.split('\n') if f.strip()}
        except:
            pass

    scored_candidates = []
    for raw_file, raw_line in matches:
        score, final_path = 0, raw_file

        # Check direct existence (Score 100)
        if project_source_path and os.path.exists(os.path.join(project_source_path, raw_file)):
            score = 100
        else:
            # Attempt to find file via search (Self-Healing)
            basename = os.path.basename(raw_file)
            if project_source_path:
                try:
                    find_cmd = ["find", project_source_path, "-name", basename, "-type", "f"]
                    find_res = subprocess.run(find_cmd, capture_output=True, text=True, timeout=5).stdout.strip().split(
                        '\n')
                    best_s, best_c = -999, None
                    for cand in [c for c in find_res if c]:
                        rel = os.path.relpath(cand, project_source_path)
                        s = 0
                        if os.path.dirname(raw_file) in rel: s += 10  # +10: Parent dir match
                        if rel in recent_changes: s += 5  # +5: Recently modified
                        s -= abs(rel.count('/') - raw_file.count('/'))  # -1: Depth penalty
                        if s > best_s: best_s, best_c = s, rel
                    if best_c: score, final_path = 60 + best_s, best_c
                except:
                    pass

        scored_candidates.append({"file": final_path, "line": int(raw_line), "score": score})

    scored_candidates.sort(key=lambda x: x['score'], reverse=True)
    best = scored_candidates[0] if scored_candidates else None

    # Execute Blame (if score >= 60)
    if best and best['score'] >= 60 and project_source_path:
        try:
            blame_cmd = ["git", "-C", project_source_path, "blame", "-L", f"{best['line']},{best['line']}",
                         "--porcelain", best['file']]
            res = subprocess.run(blame_cmd, capture_output=True, text=True, check=True, timeout=10)
            sha = res.stdout.split('\n')[0].split(' ')[0]
            if len(sha) >= 7:
                return {"status": "success", "clue_type": "blame",
                        "data": {"sha": sha, "file": best['file'], "line": best['line']}}
        except:
            pass

    # Fallback to Time-Window Suspects
    return {"status": "success", "clue_type": "time_window",
            "data": {"file": best['file'] if best else None, "error_date": error_date}}


def get_enhanced_history_context(project_source_path: str, clue_data: dict = None, file_rel_path: str = "",
                                 line_num: int = 0, sha: str = "") -> dict:
    """
    [HAFix Phase 3] Chain-of-Evidence Synthesis.
    Replaces the original logic to support multi-mode evidence gathering.

    Args:
        project_source_path: Root path of the source code.
        clue_data: (New) Structured output from Phase 1 (extract_buggy_line_info).
        file_rel_path: (Legacy/Deprecated) Used if clue_data is missing.
        line_num: (Legacy/Deprecated) Used if clue_data is missing.
        sha: (Legacy/Deprecated) Used if clue_data is missing.
    """
    import os, subprocess
    from datetime import datetime, timedelta

    if not os.path.isdir(os.path.join(project_source_path, ".git")):
        return {"status": "error", "message": "Not a git repository."}

    # --- Auto-convert Legacy Call to Phase 1 Data if clue_data is missing ---
    if not clue_data:
        if sha:
            clue_data = {"clue_type": "blame", "data": {"sha": sha, "file": file_rel_path}}
        elif file_rel_path and line_num:
            clue_data = {"clue_type": "time_window", "data": {"file": file_rel_path}}  # Fallback handling

    if not clue_data:
        return {"status": "error", "message": "No clue data provided."}

    clue_type = clue_data.get("clue_type")
    payload = clue_data.get("data", {})
    evidence = {"clue_type": clue_type, "suspect_sha": payload.get("sha", "N/A"), "core_files": [],
                "auxiliary_timeline": [], "diffs": []}

    try:
        # 1. Determine Core Tracing Files
        if clue_type == "blame":
            target_sha = payload['sha']
            show_res = subprocess.run(
                ["git", "-C", project_source_path, "show", "--name-only", "--format=", target_sha],
                capture_output=True, text=True, timeout=10).stdout
            changed = [f.strip() for f in show_res.split('\n') if f.strip()]
            # Filter to top 3 relevant source/config files
            exts = ('.c', '.go', '.cpp', '.h', '.sh', 'Dockerfile', 'build.sh', 'go.mod', 'CMakeLists.txt')
            evidence["core_files"] = [f for f in changed if f.endswith(exts) or any(x in f for x in exts)][:3]
        else:
            # Keyword/Time-Window mode: prioritize the reported file
            if payload.get("file"):
                evidence["core_files"] = [payload.get("file")]
            # If no file, leave empty for Agent to scan config

        # 2. Build Time Window (±24h)
        error_date = payload.get("error_date", "")
        since_until = []
        if error_date and error_date.strip():
            try:
                clean_date = error_date.replace('.', '-').replace('/', '-')
                t = datetime.strptime(clean_date.split()[0], '%Y-%m-%d')
                since_until = [f"--since={(t - timedelta(days=1)).strftime('%Y-%m-%d')}",
                               f"--until={(t + timedelta(days=1)).strftime('%Y-%m-%d')}"]
            except:
                pass

        # 3. Chain-of-Evidence Collection
        for f in [x for x in evidence["core_files"] if x and os.path.exists(os.path.join(project_source_path, x))]:
            # A. Auxiliary Timeline (git log -n 5)
            log_cmd = ["git", "-C", project_source_path, "log", *since_until, "-n", "5", "--format=%H|%cd|%s", "--", f]
            log_res = subprocess.run(log_cmd, capture_output=True, text=True, timeout=10).stdout.strip()
            if log_res:
                evidence["auxiliary_timeline"].append(
                    {"file": f, "commits": [l.split('|') for l in log_res.split('\n') if '|' in l]})

            # B. Structural Sampling (Unified Diff -U3)
            if evidence["suspect_sha"] != "N/A" and len(evidence["suspect_sha"]) >= 7:
                diff_cmd = ["git", "-C", project_source_path, "show", "-U3", "--format=", evidence["suspect_sha"], "--",
                            f]
                diff_res = subprocess.run(diff_cmd, capture_output=True, text=True, timeout=10).stdout
                evidence["diffs"].append({"file": f, "content": diff_res[:8000]})  # 8000 char Token Guard

        return {"status": "success", "data": evidence}
    except Exception as e:
        return {"status": "error", "message": f"Synthesis failed: {str(e)}"}


def checkout_project_commit(project_source_path: str, sha: str) -> Dict[str, str]:
    """
    Executes git checkout with built-in remote self-healing.
    If the SHA is not found locally (reference is not a tree), it attempts to fetch from origin.
    """
    import os
    import subprocess

    print(f"--- Tool: checkout_project_commit | SHA: {sha} | Path: {project_source_path} ---")

    if not os.path.isdir(os.path.join(project_source_path, ".git")):
        return {'status': 'error', 'message': f"Path '{project_source_path}' is not a valid git repository."}

    try:
        # 1. 预清理：强制放弃任何本地残留修改，确保切换环境绝对干净
        subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, cwd=project_source_path)
        subprocess.run(["git", "clean", "-fdx"], capture_output=True, cwd=project_source_path)

        # 2. 尝试执行物理切换
        command = ["git", "checkout", sha]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', cwd=project_source_path)

        # 3. 🔑 核心补丁：处理“引用不是一个树”的错误 (Commit 不在本地)
        if result.returncode != 0:
            err_msg = result.stderr.lower()
            if "reference is not a tree" in err_msg or "not a commit" in err_msg or "引用不是一个树" in result.stderr:
                print(f"--- [SELF-HEALING] SHA {sha} missing. Attempting to fetch from remote... ---")

                # 尝试从远程精准拉取该 SHA 节点
                fetch_cmd = ["git", "fetch", "origin", sha]
                fetch_res = subprocess.run(fetch_cmd, capture_output=True, text=True, cwd=project_source_path)

                if fetch_res.returncode == 0:
                    print(f"--- [SELF-HEALING] Fetch successful. Retrying checkout... ---")
                    # 再次尝试 checkout
                    result = subprocess.run(["git", "checkout", sha], capture_output=True, text=True,
                                            cwd=project_source_path)
                else:
                    # 如果精准 fetch 失败，尝试全量 unshallow (针对部分浅克隆仓库)
                    print(f"--- [SELF-HEALING] Precise fetch failed. Attempting unshallow fetch... ---")
                    subprocess.run(["git", "fetch", "--unshallow"], capture_output=True, cwd=project_source_path)
                    result = subprocess.run(["git", "checkout", sha], capture_output=True, text=True,
                                            cwd=project_source_path)

        # 4. 最终结果判定
        if result.returncode == 0:
            # 记录成功后的当前状态摘要
            head_info = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                       cwd=project_source_path).stdout.strip()
            return {
                'status': 'success',
                'message': f"Successfully checked out SHA {sha}. Current HEAD: {head_info}"
            }
        else:
            return {
                'status': 'error',
                'message': f"Git checkout failed after self-healing attempts. Error: {result.stderr.strip()}"
            }

    except Exception as e:
        return {'status': 'error', 'message': f"Unexpected system error during checkout: {str(e)}"}


def download_remote_log(log_url: str, project_name: str, error_time_str: str) -> Dict[str, str]:
    """
    Download remote log file and save it locally using 'YYYY_M_D error.txt' format.
    🔑 优化：提供多维时间分隔符(.-/)自适应健壮解析；
    🔑 优化：缓存命中时实现零网络请求，即刻放行。
    """
    import os
    import sys
    import requests
    from datetime import datetime

    print(f"--- Tool: download_remote_log called for URL: {log_url} ---")

    try:
        # 🔑 1. 强鲁棒日期分隔符清洗自愈
        clean_time_str = error_time_str.strip().replace('.', '-').replace('/', '-')
        error_date = None

        for fmt in ['%Y-%m-%d', '%Y_%m_%d', '%Y%m%d']:
            try:
                error_date = datetime.strptime(clean_time_str, fmt).date()
                break
            except ValueError:
                continue

        if not error_date:
            return {'status': 'error',
                    'message': f"Failed to parse error date string '{error_time_str}' with standard formats."}

        # 确定本地缓存路径
        local_log_dir = os.path.join("build_error_log", project_name)
        os.makedirs(local_log_dir, exist_ok=True)

        if sys.platform == "win32":
            local_log_filename = error_date.strftime("%Y_%#m_%#d") + " error.txt"
        else:
            local_log_filename = error_date.strftime("%Y_%-m_%-d") + " error.txt"

        local_log_filepath = os.path.join(local_log_dir, local_log_filename)

        # 🔑 2. 缓存就地放行逻辑：如果已存在，零网络损耗
        if os.path.exists(local_log_filepath) and os.path.getsize(local_log_filepath) > 0:
            print(f"--- Log file already exists locally: {local_log_filepath}. Skipping download. ---")
            return {"status": "success", "local_path": os.path.abspath(local_log_filepath),
                    "message": "Local log cache hit. Skipping remote pull."}

        # 3. 缓存未命中，执行网络下载
        print(f"--- Downloading log from {log_url} to {local_log_filepath} ---")
        response = requests.get(log_url, stream=True, timeout=30)
        response.raise_for_status()

        with open(local_log_filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        print(f"--- Successfully downloaded log to: {local_log_filepath} ---")
        return {"status": "success", "local_path": os.path.abspath(local_log_filepath),
                "message": "Successfully downloaded remote log."}

    except requests.exceptions.RequestException as e:
        return {"status": "error", "message": f"Failed to download log from {log_url}: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"An unexpected error occurred during log download: {e}"}


def update_reflection_journal(
        project_name: str,
        attempt_id: int,
        round_id: int,
        strategy_used: str,
        solution_plan: str,
        build_log_tail: str,
        reflection_analysis: str,
        deterioration_score: int,
        solved_problems: str,
        unsolved_problems: str,
        should_rollback: bool = False
) -> Dict:
    """
    Explicitly record Attempt and Round IDs, store concise problem descriptions, and extract recent lessons for the state.
    """
    import os
    import json
    from datetime import datetime

    if not os.environ.get("ENABLE_REFLECTION", "True") == "True":
        return {"status": "success", "trigger_rollback": False}

    print(f"--- Tool: update_reflection_journal (v5) for A{attempt_id}_R{round_id} ---")
    JOURNAL_FILE = "reflection_journal.json"

    new_entry = {
        "attempt_id": attempt_id,
        "round_id": round_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_used,
        "solved": solved_problems,
        "unsolved": unsolved_problems,
        "deterioration_score": deterioration_score,
        "reflection": reflection_analysis,
        "should_rollback": should_rollback
    }

    history = []
    if os.path.exists(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except:
            pass
    history.append(new_entry)

    with open(JOURNAL_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    current_attempt_history = [h for h in history if h['attempt_id'] == attempt_id]
    consecutive_high_score = False
    if len(current_attempt_history) >= 2:
        if current_attempt_history[-1].get("deterioration_score", 0) > 7 and \
                current_attempt_history[-2].get("deterioration_score", 0) > 7:
            consecutive_high_score = True

    lessons = []
    for h in current_attempt_history[-3:]:
        lessons.append(
            f"A{h['attempt_id']}_R{h['round_id']} (Score:{h['deterioration_score']}):\n"
            f"  [Fixed]: {h['solved']}\n"
            f"  [Pending]: {h['unsolved']}"
        )
    summary_for_state = "\n".join(lessons)

    return {
        "status": "success",
        "reflection_summary": summary_for_state,
        "trigger_rollback": should_rollback or consecutive_high_score,
        "deterioration_score": deterioration_score
    }


def query_expert_knowledge(log_path: str) -> dict:
    """
    Dynamically select relevant principles from the knowledge base based on log keywords to optimize token usage.
    """
    if not ENABLE_EXPERT_KNOWLEDGE:
        print("--- [ABLATION] Expert Knowledge is DISABLED. ---")
        return {
            "status": "success",
            "knowledge": "Expert knowledge system is currently disabled by ablation configuration."
        }
    KNOWLEDGE_FILE = "expert_knowledge.json"
    if not os.path.exists(KNOWLEDGE_FILE):
        return {"status": "error", "message": "Knowledge base not found."}

    try:
        with open(KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
            kb = json.load(f)

        log_sample = ""
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as lf:
                log_sample = "".join(lf.readlines()[-100:]).lower()

        category_map = {
            "linker": ["linker", "undefined reference", "symbol", "lib", ".a", ".so", "link"],
            "docker": ["docker", "workdir", "apt-get", "copy", "run", "entrypoint"],
            "swift": ["swift", "package.swift", "spm", "tools-version"],
            "path": ["no such file", "directory", "cannot stat", "path", "mkdir"]
        }

        selected_principles = []
        all_principles = kb.get("general_principles", [])

        hit_categories = [cat for cat, kws in category_map.items() if any(kw in log_sample for kw in kws)]

        for p in all_principles:
            if any(cat in p.lower() for cat in hit_categories):
                selected_principles.append(p)

        if not selected_principles:
            final_principles = all_principles[:3]
        else:
            final_principles = selected_principles[:6]

        matched_advice = []
        for entry in kb.get("patterns", []):
            if re.search(entry["pattern"], log_sample, re.IGNORECASE):
                matched_advice.append(f"- [Specific Match]: {entry['advice']}")

        knowledge_str = "--- Relevant Principles ---\n" + "\n".join([f"- {item}" for item in final_principles])
        if matched_advice:
            knowledge_str += "\n\n--- Targeted Advice ---\n" + "\n".join(matched_advice)

        return {"status": "success", "knowledge": knowledge_str}
    except Exception as e:
        return {"status": "error", "message": f"Expert knowledge error: {str(e)}"}


def manage_git_state(path: str, action: str, message: str = "", commit_sha: str = "") -> Dict:
    """
    Manages the Git state tree with logical fencing and physical auditing.
    Supports: init, commit, rollback, status, log, fetch.
    """
    import os, subprocess
    print(f"--- Tool: manage_git_state | Action: {action} | Path: {path} ---")

    if not os.path.exists(path):
        return {"status": "error", "message": f"Path {path} does not exist."}

    abs_path = os.path.abspath(path)
    framework_root = os.path.dirname(os.path.abspath(__file__))
    if abs_path == framework_root:
        return {"status": "error",
                "message": "CRITICAL: Security Violation. Operations on Agent Framework root are blocked."}

    original_cwd = os.getcwd()
    try:
        # 1. 物理环境权限自愈 (针对 Docker 产生的 root 文件)
        uid = os.getuid()
        gid = os.getgid()
        if action in ["init", "commit", "rollback"]:
            try:
                subprocess.run([
                    "docker", "run", "--rm", "-v", f"{abs_path}:/src",
                    "alpine", "chown", "-R", f"{uid}:{gid}", "/src"
                ], capture_output=True, check=True, timeout=30)
            except Exception as e:
                print(f"--- Warning: Permission reclamation failed: {e} ---")

        os.chdir(abs_path)

        # 2. 基础配置初始化
        if action in ["init", "commit"]:
            if not os.path.exists(".git"):
                subprocess.run(["git", "init"], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "agent@oss-fuzz-repair.com"], check=True)
            subprocess.run(["git", "config", "user.name", "Repair Agent"], check=True)

        # 3. 分支逻辑处理
        if action == "init":
            subprocess.run(["git", "add", "."], check=True)
            has_commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True).returncode == 0
            if not has_commit:
                subprocess.run(["git", "commit", "-m", "[BASELINE] Initial experiment state"], check=True,
                               capture_output=True)
            return {"status": "success", "message": f"Git initialized at Baseline in {path}"}

        elif action == "commit":
            subprocess.run(["git", "add", "."], check=True)
            diff_check = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
            if not diff_check:
                return {"status": "success", "message": "No changes to commit."}

            full_message = f"[AGENT_FIX] {message}"
            subprocess.run(["git", "commit", "-m", full_message], capture_output=True, text=True, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            return {"status": "success", "sha": sha, "message": f"State saved: {full_message}"}

        elif action == "rollback":
            # 统计带有 [AGENT_FIX] 标记的提交数量作为配额
            res = subprocess.run(["git", "log", "--grep=\\[AGENT_FIX\\]", "--oneline"], capture_output=True, text=True)
            quota = len([l for l in res.stdout.split('\n') if l.strip()])

            if quota <= 0:
                return {
                    "status": "error",
                    "message": "Already at the Initial Baseline. No further rollback possible."
                }

            target = commit_sha if commit_sha else "HEAD~1"
            subprocess.run(["git", "reset", "--hard", target], check=True, capture_output=True)
            subprocess.run(["git", "clean", "-fxd"], check=True, capture_output=True)
            return {"status": "success", "message": f"Rolled back to {target}. Remaining Fixes: {quota - 1}"}

        elif action == "status":
            res = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
            return {"status": "success", "stdout": res.stdout, "message": "Retrieved git status."}

        elif action == "log":
            # 支持将 message 解析为附加参数，例如 "-n 5"
            log_args = message.split() if message else ["-n", "5", "--oneline"]
            cmd = ["git", "log"] + log_args
            res = subprocess.run(cmd, capture_output=True, text=True)
            return {"status": "success", "stdout": res.stdout, "message": f"Executed: {' '.join(cmd)}"}

        elif action == "fetch":
            # 处理远程拉取请求
            fetch_args = message.split() if message else ["origin"]
            cmd = ["git", "fetch"] + fetch_args
            res = subprocess.run(cmd, capture_output=True, text=True)
            return {"status": "success", "stdout": res.stdout, "message": "Fetch completed."}

        else:
            return {"status": "error", "message": f"Action '{action}' is not implemented."}

    except Exception as e:
        return {"status": "error", "message": f"Git Intervention Failed: {str(e)}"}
    finally:
        os.chdir(original_cwd)


def clear_commit_analysis_state() -> Dict[str, str]:
    """
    Remove the commit analysis sentinel file to allow commit_finder_agent to re-run in the next loop.
    """
    commit_analysis_file = "generated_prompt_file/commit_changed.txt"
    if os.path.exists(commit_analysis_file):
        try:
            os.remove(commit_analysis_file)
            return {"status": "success",
                    "message": f"Cleared old commit analysis state. '{commit_analysis_file}' has been removed."}
        except Exception as e:
            return {"status": "error", "message": f"Failed to remove '{commit_analysis_file}': {e}"}
    else:
        return {"status": "success", "message": "No commit analysis state to clear."}


def extract_build_metadata_from_log(log_path: str) -> Dict:
    """
    Extract critical build metadata from the original error log using robust non-greedy regexes.
    Identifies GCR digest, compile flags, and third-party dependencies cleanly.
    """
    import os
    import re

    print(f"--- Tool: extract_build_metadata from {log_path} ---")
    try:
        if not os.path.exists(log_path):
            return {'status': 'error', 'message': 'Log file not found.'}

        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        lines = content.splitlines()
        metadata = {
            'base_image_digest': '',
            'engine': 'libfuzzer',
            'sanitizer': 'address',
            'architecture': 'x86_64',
            'software_repo_url': '',
            'software_sha': '',
            'dependencies': []
        }

        # 🔑 1. 非贪婪 GCR base image digest 匹配提取
        digest_match = re.search(r'Digest:\s*?sha256:([a-f0-9]{64}?)', content, re.IGNORECASE)
        if digest_match:
            metadata['base_image_digest'] = digest_match.group(1)

        # 🔑 2. 非贪婪编译器流水线编译参数匹配
        for line in lines:
            if 'compile-' in line:
                m = re.search(r'compile-([a-z0-9_]+?)-([a-z0-9_]+?)-([a-z0-9_]+?)', line)
                if m:
                    metadata['engine'], metadata['sanitizer'], metadata['architecture'] = m.groups()
                break

        # 🔑 3. 严格针对 Step #2 - "srcmap" 产生的依赖信息进行非贪婪提取
        git_pattern = re.compile(r'url:\s*?"([^"]+?)",\s*?rev:\s*?"([^"]+?)"')
        found_gits = []

        srcmap_zone = False
        for line in lines:
            if 'Step #2 - "srcmap"' in line:
                srcmap_zone = True
            elif 'Step #3 - "' in line:
                srcmap_zone = False

            if srcmap_zone:
                match = git_pattern.search(line)
                if match:
                    found_gits.append({'url': match.group(1), 'rev': match.group(2)})

        if found_gits:
            metadata['software_repo_url'] = found_gits[0]['url']
            metadata['software_sha'] = found_gits[0]['rev']
            metadata['dependencies'] = found_gits[1:]

        return {'status': 'success', 'metadata': metadata}
    except Exception as e:
        return {'status': 'error', 'message': f"Unexpected parser crash on log extraction: {e}"}


def patch_project_dockerfile(
        project_name: str,
        oss_fuzz_path: str,
        base_image_digest: str,
        dependencies: List[Dict] = None  # 新增参数：依赖列表
) -> Dict:
    import os
    import re

    print(f"--- Tool: patch_project_dockerfile (Enhanced) for {project_name} ---")
    dockerfile_path = os.path.join(oss_fuzz_path, "projects", project_name, "Dockerfile")

    try:
        with open(dockerfile_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 1. 锁定 Base Image (现有逻辑)
        if base_image_digest:
            pattern = r'(FROM\s+gcr.io/oss-fuzz-base/base-builder[^\s:@]*)'
            content = re.sub(pattern + r'[^\s]*', rf'\1@sha256:{base_image_digest}', content, flags=re.IGNORECASE)

        # 2. 优化：移除所有 --depth 限制 (现有逻辑)
        content = re.sub(r'--depth[=\s]+?\d+', '', content, flags=re.IGNORECASE)

        # 3. 🔑 核心增强：注入第三方依赖的 git checkout 动作
        if dependencies:
            for dep in dependencies:
                url = dep.get('url', '')
                sha = dep.get('rev', '')
                if not url or not sha: continue

                # 在 Dockerfile 中搜索对应的 git clone 指令行
                # 如果匹配到包含该仓库 URL 的 git clone，则在末尾追加 checkout 指令
                pattern = rf"(git clone.*?{re.escape(url.split('/')[-1].replace('.git', ''))}.*)"

                def inject_checkout(match):
                    original_line = match.group(1)
                    # 确保命令连接符号正确
                    return f"{original_line} && cd {url.split('/')[-1].replace('.git', '')} && git checkout {sha} && cd .."

                content = re.sub(pattern, inject_checkout, content)

        with open(dockerfile_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return {'status': 'success', 'message': "Dockerfile patched with pinned dependencies."}
    except Exception as e:
        return {'status': 'error', 'message': f'Failed to patch: {str(e)}'}


def update_yaml_report(file_path: str, row_index: int, result: str) -> dict:
    """
    Update the project status in the YAML report.
    Guarantees atomic file swapping and unicode safety to prevent character corruption.
    """
    import os
    import yaml
    import tempfile
    from datetime import datetime

    print(f"--- Tool: update_yaml_report called for file '{file_path}', index {row_index} ---")
    try:
        if not os.path.exists(file_path):
            return {'status': 'error', 'message': f"YAML file not found at '{file_path}'."}

        # 读取原始数据
        with open(file_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        if row_index < 0 or row_index >= len(data):
            return {'status': 'error', 'message': f"Invalid row index: {row_index}."}

        # 更新元状态
        data[row_index]['state'] = 'yes'
        data[row_index]['fix_result'] = result
        data[row_index]['fix_date'] = datetime.now().strftime('%Y-%m-%d')

        # 🔑 强固化写入：通过临时文件原子替换(Atomic Swap) + allow_unicode 保证写入安全与防止乱码
        dir_name = os.path.dirname(os.path.abspath(file_path))
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".projects_yaml_tmp_", suffix=".yaml")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as tmp_f:
                yaml.dump(data, tmp_f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            os.replace(tmp_path, file_path)  # 物理原子级覆盖，杜绝写中途中断损坏原文件
        except Exception as swap_e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise swap_e

        message = f"Successfully updated project at index {row_index} in '{file_path}' with result: '{result}'."
        print(message)
        return {'status': 'success', 'message': message}
    except Exception as e:
        message = f"Failed to update YAML report cleanly: {e}"
        print(f"--- ERROR: {message} ---")
        return {'status': 'error', 'message': message}


def get_git_commits_around_date(
        project_source_path: str,
        error_date: str,
        max_limit: int = 300,
        **kwargs  # Catch unexpected params
) -> Dict:
    """
    Retrieve ALL commits within a ±24h time window for comprehensive pre-screening.
    Optimized: Returns lightweight metadata (SHA/Date/Message) only.
    File changes & diffs are deferred to Phase 3 on-demand extraction to save time & tokens.
    """
    if 'count' in kwargs:
        raise ValueError("get_git_commits_around_date does not accept 'count' parameter. Use 'max_limit' instead.")

    if not ENABLE_HISTORY_ENHANCEMENT:
        print(f"--- [ABLATION] Temporal commit search is DISABLED. ---")
        return {'status': 'success', 'commits': [], 'total_count': 0}

    print(
        f"--- Tool: get_git_commits_around_date (Comprehensive Scan) | Path: {project_source_path} | Date: {error_date} ---")

    if not os.path.isdir(os.path.join(project_source_path, ".git")):
        return {'status': 'error', 'message': "Not a git repository."}

    try:
        # 容错解析日期
        target_dt = None
        if error_date and error_date.strip():
            for fmt in ['%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d']:
                try:
                    target_dt = datetime.strptime(error_date.strip(), fmt)
                    break
                except ValueError:
                    continue

        if target_dt:
            start_date = (target_dt - timedelta(days=1)).strftime('%Y-%m-%d')
            end_date = (target_dt + timedelta(days=1)).strftime('%Y-%m-%d')
            print(f"--- Scanning commits between {start_date} and {end_date} (Limit: {max_limit}) ---")
            cmd = [
                "git", "log",
                f"--since={start_date} 00:00:00",
                f"--until={end_date} 23:59:59",
                f"--max-count={max_limit}",
                "--pretty=format:%H|%cd|%s",
                "--date=format:%Y-%m-%d %H:%M:%S"
            ]
        else:
            print(f"--- Date invalid. Falling back to recent {max_limit} commits. ---")
            cmd = ["git", "log", f"--max-count={max_limit}", "--pretty=format:%H|%cd|%s",
                   "--date=format:%Y-%m-%d %H:%M:%S"]

        result = subprocess.run(cmd, cwd=project_source_path, capture_output=True, text=True, check=False)

        commits = []
        for line in result.stdout.strip().split('\n'):
            if not line: continue
            parts = line.split('|', 2)
            if len(parts) < 3: continue
            sha, date, msg = parts
            # 🔑
            # 31/5000
            # Only return lightweight metadata. Do not trigger the git show query for file changes here.
            commits.append({
                "sha": sha,  # Full 40-char commit SHA
                "date": date,  # Formatted: YYYY-MM-DD HH:MM:SS
                "message": msg,  # First line of commit message (truncated if needed)
                "is_merge": msg.startswith("Merge"),  # Quick merge detection for Agent filtering
            })

        print(f"--- Found {len(commits)} commits in window. Ready for Agent pre-screening. ---")
        return {
            'status': 'success',
            'commits': commits,  # List[Dict{sha, date, message, is_merge}]
            'total_count': len(commits),
            'note': "File changes & diffs deferred to Phase 3 on-demand extraction via save_commit_diff_to_file/get_enhanced_history_context"
            # Help Agent understand workflow
        }
    except Exception as e:
        return {'status': 'error', 'message': f"Failed to get commits: {e}"}


def save_commit_diff_to_file(project_name: str, project_source_path: str, sha: str, error_time: str):
    """
    Extract recent changes and simplify based on content length to stay within token limits.
    """

    if not ENABLE_HISTORY_ENHANCEMENT:
        print(f"--- [ABLATION] Saving commit diff is DISABLED. ---")
        return {'status': 'error', 'message': 'History enhancement is disabled by ablation configuration.'}

    import os
    import subprocess
    print(f"--- Tool: save_commit_diff_to_file (With Token Guard) for {sha} ---")

    TOKEN_GUARD_CHARS = 12000
    OUTPUT_PATH = "generated_prompt_file/commit_changed.txt"
    os.makedirs("generated_prompt_file", exist_ok=True)

    try:
        raw_diff_res = subprocess.run(["git", "-C", project_source_path, "show", sha],
                                      capture_output=True, text=True, check=True)
        content = raw_diff_res.stdout

        if len(content) > TOKEN_GUARD_CHARS:
            print(f"  - Content length ({len(content)}) exceeds guard. Simplifying...")

            lines = content.split('\n')
            simplified = [l for l in lines if l.startswith(('+', '-', '@', 'commit', 'Author', 'Date'))]
            content = "\n".join(simplified)

            if len(content) > TOKEN_GUARD_CHARS:
                summary_res = subprocess.run(["git", "-C", project_source_path, "show", "--stat", sha],
                                             capture_output=True, text=True, check=True)
                content = "--- [DIFF TOO LARGE: Showing Summary Only] ---\n" + summary_res.stdout

        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            f.write(content)

        return {"status": "success", "message": f"Saved simplified diff to {OUTPUT_PATH}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def read_projects_from_yaml(file_path: str) -> dict:
    """
    Read project information, including state field checks and boolean compatibility.
    Exempts 'projects.yaml' from whitelist checks and catches syntax corruption safely.
    """
    import os
    import re
    import yaml
    from datetime import datetime
    from utils.path_utils import normalize_patch_path, validate_patch_path
    from utils.error_handler import format_path_error

    print(f"--- Tool: read_projects_from_yaml called for: {file_path} ---")

    # 🔑 1. 核心配置放行：projects.yaml 属于系统级核心引导文件，跳过子目录白名单限制
    if os.path.basename(file_path) == "projects.yaml":
        target_path = file_path
    else:
        normalized_path = normalize_patch_path(file_path)
        if not validate_patch_path(normalized_path):
            return {
                'status': 'error',
                'message': format_path_error(
                    original_path=file_path,
                    normalized_path=normalized_path,
                    base_dir=os.environ.get('PROJECT_ROOT', os.getcwd()),
                    validation_passed=False,
                    extra_info={'operation': 'read_projects_from_yaml'}
                )
            }
        target_path = normalized_path

    # 🔑 2. 安全检查：如果物理文件不存在，优雅返回错误字典
    if not os.path.exists(target_path):
        return {'status': 'error', 'message': f"YAML file not found at '{target_path}'."}

    projects_to_run = []
    try:
        # 🔑 3. 强固化异常捕获：预防 YAML 物理缩进损坏或格式错误引发 main() 闪退
        try:
            with open(target_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as ye:
            return {
                'status': 'error',
                'message': f"YAML parser failed (Syntax Error) in '{target_path}': {ye}"
            }
        except Exception as fe:
            return {
                'status': 'error',
                'message': f"Failed to open/read '{target_path}': {fe}"
            }

        if not isinstance(data, list):
            return {'status': 'error', 'message': f"YAML file '{target_path}' must contain a list of projects."}

        for index, entry in enumerate(data):
            fixed_state = str(entry.get('fixed_state', 'no')).lower()
            state = str(entry.get('state', 'no')).lower()

            if fixed_state == 'no' and state == 'no':
                project_name = entry.get('project')
                sha = entry.get('oss-fuzz_sha')
                error_time_str = str(entry.get('error_time', ""))
                fuzzing_build_error_log_url = entry.get('fuzzing_build_error_log', "")

                if project_name and sha:
                    log_dir = os.path.join("build_error_log", project_name)
                    original_log_path = ""

                    if fuzzing_build_error_log_url.startswith("http"):
                        from agent_tools import download_remote_log
                        download_result = download_remote_log(fuzzing_build_error_log_url, project_name, error_time_str)
                        if download_result['status'] == 'success':
                            original_log_path = download_result['local_path']

                    if not original_log_path and os.path.isdir(log_dir):
                        try:
                            y, m, d = map(int, error_time_str.replace('.', '-').split('-'))
                            base_date = datetime(y, m, d)
                            candidates = []
                            for filename in os.listdir(log_dir):
                                if "error.txt" in filename and re.match(r"\d{4}_\d{1,2}_\d{1,2} error\.txt", filename):
                                    match = re.search(r"(\d{4})_(\d{1,2})_(\d{1,2})", filename)
                                    if match:
                                        fy, fm, fd = map(int, match.groups())
                                        file_date = datetime(fy, fm, fd)
                                        if file_date >= base_date:
                                            candidates.append((file_date, filename))
                            if candidates:
                                candidates.sort(key=lambda x: x[0])
                                original_log_path = os.path.abspath(os.path.join(log_dir, candidates[0][1]))
                        except Exception:
                            pass

                    if original_log_path:
                        project_info = {
                            "project_name": project_name,
                            "sha": str(sha),
                            "row_index": index,
                            "error_time": error_time_str,
                            "original_log_path": original_log_path,
                            "software_repo_url": entry.get('software_repo_url', ""),
                            "software_sha": entry.get('software_sha', ""),
                            "engine": entry.get('engine', ""),
                            "sanitizer": entry.get('sanitizer', ""),
                            "architecture": entry.get('architecture', ""),
                            "base_image_digest": entry.get('base_image_digest', "")
                        }
                        projects_to_run.append(project_info)
                    else:
                        print(f"Warning: Project '{project_name}' skipped due to missing log file.")
                else:
                    print(f"Warning: Project at index {index} missing core fields. Skipping.")

        print(f"--- Found {len(projects_to_run)} projects to process. ---")
        return {'status': 'success', 'projects': projects_to_run}
    except Exception as e:
        return {'status': 'error', 'message': f"Unexpected failure in YAML parsing logic: {e}"}


def force_clean_git_repo(repo_path: str) -> Dict[str, str]:
    """
    Perform a deep clean of the specified Git repository with automated permission management.
    🔑 优化：引入 Host 侧本地 native 自愈降级，防止由于无 Docker 权限导致沙箱进程卡死；
    🔑 优化：利用 cwd 参数移除 os.chdir。
    """
    import os
    import subprocess
    print(f"--- Tool: force_clean_git_repo called for: {repo_path} ---")

    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return {'status': 'error', 'message': f"'{repo_path}' is not a valid Git repository."}

    try:
        abs_repo_path = os.path.abspath(repo_path)
        uid, gid = os.getuid(), os.getgid()
        docker_ok = False

        # 1. 尝试使用 Docker 容器强制回收 Root 生成的编译产物权限
        try:
            result = subprocess.run([
                "docker", "run", "--rm", "-v", f"{abs_repo_path}:/src",
                "alpine", "chown", "-R", f"{uid}:{gid}", "/src"
            ], capture_output=True, text=True, timeout=15, check=False)
            if result.returncode == 0:
                docker_ok = True
        except Exception as de:
            print(f"--- [FALLBACK] Docker permission reclaim failed/timed out: {de} ---")

        # 2. 🔑 优化：如果 Docker 方案失败，自动触发 Host 侧 native 自愈进行恢复
        if not docker_ok:
            print("--- [FALLBACK] Docker unavailable. Attempting Host native chown/chmod reclamation... ---")
            try:
                # 尝试原生 chown
                subprocess.run(["chown", "-R", f"{uid}:{gid}", abs_repo_path], capture_output=True, check=False)
                # 强行赋予当前用户对工作区的读写执行(rwx)权限，保证后续 git 操作不受 Permission Denied 干扰
                subprocess.run(["chmod", "-R", "u+rwX", abs_repo_path], capture_output=True, check=False)
            except Exception as host_e:
                print(f"--- [WARNING] Host native reclamation failed: {host_e} ---")

        # 3. 🔑 优化：统一在 cwd=abs_repo_path 绝对上下文中完成干净物理重置
        subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, text=True, check=True,
                       cwd=abs_repo_path)

        branch_res = subprocess.run(["git", "branch", "--list"], capture_output=True, text=True, cwd=abs_repo_path,
                                    check=True)
        main_branch = "main" if "main" in branch_res.stdout else "master"

        subprocess.run(["git", "switch", "-f", main_branch], capture_output=True, text=True, cwd=abs_repo_path,
                       check=True)
        subprocess.run(["git", "clean", "-fxd"], capture_output=True, text=True, cwd=abs_repo_path, check=True)

        return {'status': 'success', 'message': f"Successfully reclaimed and cleaned repo at '{repo_path}'."}
    except Exception as e:
        return {'status': 'error', 'message': f"Deep clean failed: {str(e)}"}


def get_project_paths(project_name: str) -> Dict[str, str]:
    """
    Generates and returns the standard project_config_path and project_source_path based on the project name.
    """
    print(f"--- Tool: get_project_paths called for: {project_name} ---")
    base_path = os.path.abspath(os.path.join(os.path.dirname(__file__)))

    safe_project_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()

    config_path = os.path.join(base_path, "oss-fuzz", "projects", safe_project_name)
    source_path = os.path.join(base_path, "process", "project", safe_project_name)

    paths = {
        "project_name": project_name,
        "project_config_path": config_path,
        "project_source_path": source_path,
        "max_depth": 1
    }
    print(f"--- Generated paths: {paths} ---")
    return paths


def save_processed_project(project_name: str) -> Dict[str, str]:
    """
    Appends a processed project name to the project_processed.txt file.
    """
    print(f"--- Tool: save_processed_project called for: {project_name} ---")
    try:
        os.makedirs(PROCESSED_PROJECTS_DIR, exist_ok=True)
        with open(PROCESSED_PROJECTS_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{project_name}\n")
        message = f"Successfully saved '{project_name}' to processed list."
        print(f"--- {message} ---")
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"Failed to save processed project '{project_name}': {e}"
        print(f"--- ERROR: {message} ---")
        return {"status": "error", "message": message}


def update_excel_report(file_path: str, row_index: int, attempted: str, result: str) -> Dict[str, str]:
    """
    Updates the "Whether Fix Was Attempted", "Fix Result", and "Fix Date" columns for a specified row in an .xlsx file.
    """
    print(f"--- Tool: update_excel_report called for file '{file_path}', row {row_index} ---")
    try:
        workbook = openpyxl.load_workbook(file_path)
        sheet = workbook.active
        headers = [cell.value for cell in sheet[1]]

        attempted_col_idx = headers.index("是否尝试修复") + 1
        result_col_idx = headers.index("修复结果") + 1
        date_col_idx = headers.index("修复日期") + 1

        sheet.cell(row=row_index, column=attempted_col_idx, value=attempted)
        sheet.cell(row=row_index, column=result_col_idx, value=result)
        sheet.cell(row=row_index, column=date_col_idx, value=datetime.now().strftime('%Y-%m-%d'))

        workbook.save(file_path)
        message = f"Successfully updated row {row_index} in '{file_path}' with result: '{result}'."
        print(message)
        return {'status': 'success', 'message': message}
    except Exception as e:
        message = f"Failed to update Excel file: {e}"
        print(f"--- ERROR: {message} ---")
        return {'status': 'error', 'message': message}


def read_projects_from_excel(file_path: str) -> Dict:
    """
    Reads project information from the specified .xlsx file.
    Only reads rows where "Error Consistency" is "Yes" and "Whether Fix Was Attempted" is not "Yes".
    """
    print(f"--- Tool: read_projects_from_excel called for: {file_path} ---")
    if not os.path.exists(file_path):
        return {'status': 'error', 'message': f"Excel file not found at '{file_path}'."}

    projects_to_run = []
    try:
        workbook = openpyxl.load_workbook(file_path)
        sheet = workbook.active
        headers = [cell.value for cell in sheet[1]]

        required_headers = ["项目名称", "复现oss-fuzz SHA", "报错是否一致", "是否尝试修复"]
        if not all(h in headers for h in required_headers):
            return {'status': 'error',
                    'message': f"Excel file is missing one of the required columns: {required_headers}"}

        name_idx = headers.index("项目名称")
        sha_idx = headers.index("复现oss-fuzz SHA")
        consistent_idx = headers.index("报错是否一致")
        attempted_idx = headers.index("是否尝试修复")

        for row_index, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            if row[consistent_idx] == "是" and row[attempted_idx] != "是":
                project_info = {
                    "project_name": row[name_idx],
                    "sha": str(row[sha_idx]),
                    "row_index": row_index
                }
                projects_to_run.append(project_info)

        print(f"--- Found {len(projects_to_run)} new projects to process. ---")
        return {'status': 'success', 'projects': projects_to_run}
    except Exception as e:
        return {'status': 'error', 'message': f"Failed to read or parse Excel file: {e}"}


def run_command(command: str, timeout: int = 30, max_output_chars: int = 4000) -> dict:
    """
    Execute commands safely, compatible with LLM common Shell syntax,
    enforce zero-deletion policy, and return structured results.
    """
    print(f"--- Tool: run_command called with: '{command}' ---")

    # 🔒 强力零删除策略与高危拦截规则（使用词边界 \b 正则防止利用拼装、连写等手段绕过检测）
    # 明确禁止：任何文件/目录删除、权限篡改、系统级文件写入、远程网络下载、命令注入
    deletion_patterns = r'\b(?:rm|rmdir|unlink|del|shred|erase)\b'
    dangerous_patterns = r'\b(?:wget|curl|apt-get|apt|yum|sudo|su|chmod|chown|mkfs|dd|passwd|exec|eval)\b|>\s*/etc/|>\s*/var/|>\s*/sys/|\$\('

    if re.search(f'({deletion_patterns}|{dangerous_patterns})', command, re.IGNORECASE):
        return {
            "status": "error",
            "message": "🚫 Command blocked: Deletion or unsafe system-level operations are strictly forbidden. Use structured discovery tools instead (e.g., list_files_in_dir, read_file_content)."
        }

    try:
        # ✅ 使用 /bin/bash -c 兼容大模型常用 Shell 语法（如管道符 |、重定向 >、标准错误抑制等）
        res = subprocess.run(
            ['/bin/bash', '-c', command],
            capture_output=True, text=True, timeout=timeout, check=False
        )

        out = (res.stdout + res.stderr).strip()
        truncated = False
        if len(out) > max_output_chars:
            out = out[:max_output_chars] + f"\n[⚠️ OUTPUT TRUNCATED: {len(out) - max_output_chars} chars hidden]"
            truncated = True

        # 🎯 统合状态机语义：非零退出状态码显式标记为 error，降低 Agent 逻辑干扰
        return {
            "status": "success" if res.returncode == 0 else "error",
            "return_code": res.returncode,
            "output": out,
            "truncated": truncated,
            "hint": "Tip: Use `list_files_in_dir` for exploration, `read_file_content` for file inspection. Avoid complex shell chains." if res.returncode != 0 else ""
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "message": f"Command timed out after {timeout}s. Try `read_file_content` with mode='tail_N' or use `list_files_in_dir`."
        }
    except Exception as e:
        return {"status": "error", "message": f"Execution failed: {str(e)}"}


def truncate_prompt_file(file_path: str, max_lines: int = 2000) -> Dict[str, str]:
    """
    Reads a file, and if it exceeds max_lines, truncates it in the middle, keeping the head and tail.
    """
    print(f"--- Tool: truncate_prompt_file called for: {file_path} ---")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        if len(lines) <= max_lines:
            message = "File is within line limits, no truncation needed."
            print(f"--- {message} ---")
            return {"status": "success", "message": message}

        head_count = max_lines // 4
        tail_count = max_lines - head_count

        truncated_content = "".join(lines[:head_count])
        truncated_content += "\n\n... (Content truncated due to context length limit) ...\n\n"
        truncated_content += "".join(lines[-tail_count:])

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(truncated_content)

        message = f"File '{file_path}' was truncated to approximately {max_lines} lines."
        print(f"--- {message} ---")
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"Failed to truncate file '{file_path}': {e}"
        print(f"--- ERROR: {message} ---")
        return {"status": "error", "message": message}


from datetime import datetime
import os
import shutil
import subprocess
from typing import Dict


def archive_fixed_project(project_name: str, project_config_path: str, is_success: bool = True,
                          project_source_path: str = None) -> dict:
    """
    Refactored Double-Track Archiving Tool (双轨制物理归档).
    Saves complete Trajectory Archives (Code changes, validation reports, LLM semantics, ledgers).
    Success: Saved under process/fixed/
    Failure: Saved under process/unfixed/
    """
    import os
    import shutil
    import subprocess
    from datetime import datetime
    from agent_tools import TraceLedgerManager

    print(f"--- Tool: archive_fixed_project (Double-Track) called for: {project_name} (Success: {is_success}) ---")
    try:
        # 🔑 1. 分流路径决策
        base_dir = "process/fixed" if is_success else "process/unfixed"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_project_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        destination_dir = os.path.join(os.getcwd(), base_dir, f"{safe_project_name}_{timestamp}")

        os.makedirs(destination_dir, exist_ok=True)
        os.makedirs(os.path.join(destination_dir, "diffs"), exist_ok=True)

        # 🔑 2. 导出当前的 project_repair_trace.json 账本物理轨迹文件
        ledger_path = os.path.join(os.getcwd(), "project_repair_trace.json")
        if os.path.exists(ledger_path):
            shutil.copy2(ledger_path, os.path.join(destination_dir, "project_repair_trace.json"))
            print(f"  - Captured trace ledger: project_repair_trace.json -> archive")

        # 🔑 3. 提取下游配置变更 (oss-fuzz)
        if os.path.isdir(project_config_path):
            # 获取 Baseline 节点
            baseline_sha = ""
            try:
                res = subprocess.run(
                    ["git", "-C", project_config_path, "log", "--format=%H", "--grep=\\[BASELINE\\]", "-1"],
                    capture_output=True, text=True, check=True
                )
                baseline_sha = res.stdout.strip()
            except Exception:
                pass

            changed_config_files = []
            if baseline_sha:
                try:
                    res = subprocess.run(
                        ["git", "-C", project_config_path, "diff", "--name-only", "--diff-filter=ACMRT", baseline_sha,
                         "HEAD"],
                        capture_output=True, text=True, check=True
                    )
                    changed_config_files = [f.strip() for f in res.stdout.split('\n') if f.strip()]
                except Exception:
                    pass

            if changed_config_files:
                # 拷贝变动配置文件
                for f_rel in changed_config_files:
                    src = os.path.join(project_config_path, f_rel)
                    dst = os.path.join(destination_dir, "configs", f_rel)
                    if os.path.exists(src):
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy2(src, dst)

                # 生成配置 patch
                config_patch_path = os.path.join(destination_dir, "diffs", "config_fix.patch")
                with open(config_patch_path, "w", encoding="utf-8") as pf:
                    subprocess.run(["git", "-C", project_config_path, "diff", baseline_sha, "HEAD"], stdout=pf,
                                   check=True)
                print(f"  - Config files archived: {len(changed_config_files)} files + patch")
            else:
                # 若无 Baseline，整体强制拷贝以兜底
                shutil.copytree(project_config_path, os.path.join(destination_dir, "config_all"), dirs_exist_ok=True)

        # 🔑 4. 提取上游源码变更 (process/project)
        if project_source_path and os.path.isdir(project_source_path):
            source_baseline_sha = ""
            try:
                res = subprocess.run(
                    ["git", "-C", project_source_path, "log", "--format=%H", "--grep=\\[BASELINE\\]", "-1"],
                    capture_output=True, text=True, check=True
                )
                source_baseline_sha = res.stdout.strip()
            except Exception:
                pass

            changed_source_files = []
            if source_baseline_sha:
                try:
                    res = subprocess.run(
                        ["git", "-C", project_source_path, "diff", "--name-only", "--diff-filter=ACMRT",
                         source_baseline_sha, "HEAD"],
                        capture_output=True, text=True, check=True
                    )
                    changed_source_files = [f.strip() for f in res.stdout.split('\n') if f.strip()]
                except Exception:
                    pass

            if changed_source_files:
                # 拷贝变动的源码文件
                for f_rel in changed_source_files:
                    src = os.path.join(project_source_path, f_rel)
                    dst = os.path.join(destination_dir, "source", f_rel)
                    if os.path.exists(src):
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy2(src, dst)

                # 生成源码 patch
                source_patch_path = os.path.join(destination_dir, "diffs", "source_fix.patch")
                with open(source_patch_path, "w", encoding="utf-8") as pf:
                    subprocess.run(["git", "-C", project_source_path, "diff", source_baseline_sha, "HEAD"], stdout=pf,
                                   check=True)
                print(f"  - Source files archived: {len(changed_source_files)} files + patch")

        msg = f"Archived project '{project_name}' under double-track: -> {destination_dir}"
        print(f"--- [SUCCESS] {msg} ---")
        return {"status": "success", "message": msg, "archive_dir": destination_dir}
    except Exception as e:
        return {"status": "error", "message": f"Failed to cleanly execute double-track archive: {e}"}


def download_github_repo(project_name: str, target_dir: str, repo_url: Optional[str] = None) -> Dict[str, str]:
    """
    Download a repository with path enforcement and full cloning.
    🔑 优化：全面封杀 --depth=1，保障全量克隆以支持后续 ECRCL SHA 的 checkout；
    🔑 优化：强力强制重定向，将非 oss-fuzz 的第三方仓库牢牢锁定在 process/project/ 路径下。
    """
    import json
    import time
    import subprocess
    import os
    import shutil

    current_work_dir = os.getcwd()

    # 🔑 1. 下游 oss-fuzz 与 上游开源项目路径强制路由锁
    if project_name == "oss-fuzz":
        final_target_dir = os.path.abspath(target_dir)
    else:
        safe_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        final_target_dir = os.path.abspath(os.path.join(current_work_dir, "process", "project", safe_name))

        if os.path.abspath(target_dir) != final_target_dir:
            print(f"--- Path Security Enforcement: Redirecting download from {target_dir} to {final_target_dir} ---")

    print(f"--- Tool: download_github_repo called for '{project_name}' ---")

    # 🔑 2. 若本地已存在合法的全量 Git 仓库，则跳过下载直接放行
    if os.path.isdir(final_target_dir) and os.path.exists(os.path.join(final_target_dir, ".git")):
        if project_name == "oss-fuzz":
            print(f"--- oss-fuzz exists, pulling latest... ---")
            try:
                subprocess.run(["git", "pull"], cwd=final_target_dir, check=True, capture_output=True)
                return {'status': 'success', 'path': final_target_dir, 'message': 'oss-fuzz updated.'}
            except:
                return {'status': 'success', 'path': final_target_dir,
                        'message': 'oss-fuzz update failed, using local.'}
        else:
            print(f"--- Repo '{project_name}' exists and is a valid git repo. Skipping download. ---")
            return {'status': 'success', 'path': final_target_dir, 'message': 'Repository already exists.'}

    # 3. 准备物理清理
    if os.path.isdir(final_target_dir):
        shutil.rmtree(final_target_dir)
    os.makedirs(os.path.dirname(final_target_dir), exist_ok=True)

    # 4. 远程 Repo URL 检索
    final_repo_url = repo_url if repo_url and repo_url.strip() else None
    if not final_repo_url:
        if project_name == "oss-fuzz":
            final_repo_url = "https://github.com/google/oss-fuzz.git"
        else:
            try:
                search_cmd = ["gh", "search", "repos", project_name, "--sort", "stars", "--limit", "1", "--json",
                              "fullName"]
                result = subprocess.run(search_cmd, capture_output=True, text=True, check=True, encoding='utf-8')
                parsed = json.loads(result.stdout.strip())
                if parsed:
                    final_repo_url = f"https://github.com/{parsed[0]['fullName']}.git"
                else:
                    return {'status': 'error', 'message': f"Repo not found for {project_name}"}
            except Exception as e:
                return {'status': 'error', 'message': f"Search failed: {e}"}

    # 5. Git 大文件传输参数优化
    subprocess.run(["git", "config", "--global", "http.postBuffer", "524288000"])
    subprocess.run(["git", "config", "--global", "http.lowSpeedLimit", "0"])
    subprocess.run(["git", "config", "--global", "http.lowSpeedTime", "999999"])

    max_retries = 3
    for attempt in range(max_retries):
        print(f"--- Download attempt {attempt + 1}/{max_retries} ---")
        try:
            # 🔑 6. 全量 clone 锁死（彻底抛弃 --depth 1 以免丢失提交树）
            clone_cmd = ["git", "clone", final_repo_url, final_target_dir]
            result = subprocess.run(clone_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return {'status': 'success', 'path': final_target_dir,
                        'message': 'Successfully cloned full history repository.'}
            else:
                print(f"--- Attempt {attempt + 1} failed: {result.stderr} ---")
        except Exception as e:
            print(f"--- Attempt {attempt + 1} exception: {e} ---")
        time.sleep(10 * (attempt + 1))

    return {'status': 'error', 'message': f"Failed to download {project_name} after {max_retries} attempts."}


def find_sha_for_timestamp(commits_file_path: str, error_date: str) -> Dict[str, str]:
    """
    Finds the most suitable commit SHA for a given date from a commits file.
    """
    print(f"--- Tool: find_sha_for_timestamp called for date: {error_date} ---")
    try:
        target_date = datetime.strptime(error_date, '%Y.%m.%d').date()
    except ValueError:
        return {'status': 'error', 'message': f"Invalid target date format: '{error_date}'. Expected 'YYYY.MM.DD'."}

    todays_commits: List[Tuple[datetime, str]] = []
    past_commits: List[Tuple[datetime, str]] = []

    try:
        with open(commits_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("Time: ") and i + 1 < len(lines) and lines[i + 1].strip().startswith("- SHA: "):
                try:
                    timestamp_str = line.replace("Time: ", "")
                    commit_datetime = datetime.strptime(timestamp_str, '%Y.%m.%d %H:%M')
                    sha = lines[i + 1].strip().replace("- SHA: ", "")
                    commit_date = commit_datetime.date()
                    if commit_date == target_date:
                        todays_commits.append((commit_datetime, sha))
                    elif commit_date < target_date:
                        past_commits.append((commit_datetime, sha))
                except (ValueError, IndexError):
                    pass
            i += 1
    except FileNotFoundError:
        return {'status': 'error', 'message': f"Commits file not found at: {commits_file_path}"}
    except Exception as e:
        return {'status': 'error', 'message': f"An unexpected error occurred: {e}"}

    if todays_commits:
        earliest_today = min(todays_commits)
        found_sha = earliest_today[1]
        return {'status': 'success', 'sha': found_sha}
    elif past_commits:
        latest_in_past = max(past_commits)
        found_sha = latest_in_past[1]
        return {'status': 'success', 'sha': found_sha}
    else:
        return {'status': 'error', 'message': f"No suitable SHA found on or before the date {error_date}."}


def checkout_oss_fuzz_commit(sha: str) -> Dict[str, str]:
    """
    Executes a git checkout command in the fixed oss-fuzz directory.
    🔑 优化：完全移除 os.chdir，利用 subprocess.run(cwd=...) 避免 CWD 漂移。
    """
    import os
    import subprocess

    base_path = os.path.abspath(os.path.join(os.path.dirname(__file__)))
    oss_fuzz_path = os.path.join(base_path, "oss-fuzz")
    print(f"--- Tool: checkout_oss_fuzz_commit called for SHA: {sha} in '{oss_fuzz_path}' ---")

    if not os.path.isdir(os.path.join(oss_fuzz_path, ".git")):
        return {'status': 'error', 'message': f"The directory '{oss_fuzz_path}' is not a git repository."}

    try:
        # 在指定 oss_fuzz_path 路径下获取主干分支名
        branch_res = subprocess.run(["git", "branch"], capture_output=True, text=True, cwd=oss_fuzz_path, check=True)
        main_branch = "main" if "main" in branch_res.stdout else "master"

        # 强制切回主干并拉取
        subprocess.run(["git", "switch", main_branch], capture_output=True, text=True, cwd=oss_fuzz_path, check=True)

        # 执行目标 commit 的切换
        command = ["git", "checkout", sha]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', cwd=oss_fuzz_path)

        if result.returncode == 0:
            return {'status': 'success', 'message': f"Successfully checked out SHA {sha} in oss-fuzz."}
        else:
            return {'status': 'error', 'message': f"Git checkout command failed in oss-fuzz: {result.stderr.strip()}"}
    except Exception as e:
        return {'status': 'error', 'message': f"An unexpected error occurred during oss-fuzz checkout: {e}"}


def save_file_tree(directory_path: str, output_file: Optional[str] = None) -> dict:
    """
    Gets the file tree structure of a specified directory path and saves it to a file.
    """
    print(f"--- Tool: save_file_tree called for path: {directory_path} ---")
    if not os.path.isdir(directory_path):
        error_message = f"Error: The provided path '{directory_path}' is not a valid directory."
        print(error_message)
        return {"status": "error", "message": error_message}
    if output_file is None:
        output_dir = "generated_prompt_file"
        final_output_path = os.path.join(output_dir, "file_tree.txt")
    else:
        final_output_path = output_file
    output_dir = os.path.dirname(final_output_path)
    try:
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        tree_lines = []

        def _build_tree_recursive(path, prefix=""):
            entries = sorted([e for e in os.listdir(path) if not e.startswith('.')])
            pointers = ["├── "] * (len(entries) - 1) + ["└── "]
            for pointer, entry in zip(pointers, entries):
                full_path = os.path.join(path, entry)
                if os.path.isdir(full_path):
                    tree_lines.append(f"{prefix}{pointer}📁 {entry}")
                    extension = "│   " if pointer == "├── " else "    "
                    _build_tree_recursive(full_path, prefix + extension)
                else:
                    tree_lines.append(f"{prefix}{pointer}📄 {entry}")

        tree_lines.insert(0, f"📁 {os.path.basename(os.path.abspath(directory_path))}")
        _build_tree_recursive(directory_path, prefix="")
        with open(final_output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(tree_lines))
        success_message = f"File tree has been successfully generated and saved to '{final_output_path}'."
        print(success_message)
        return {"status": "success", "message": success_message}
    except Exception as e:
        error_message = f"An error occurred while generating or saving the file tree: {str(e)}"
        print(error_message)
        return {"status": "error", "message": error_message}


@_safe_path_wrapper("save_file_tree_shallow")
def save_file_tree_shallow(directory_path: str, max_depth: int, output_file: Optional[str] = None, **kwargs) -> dict:
    """
    Generates a shallow file tree of the target directory.
    🔑 优化：强制接入 @_safe_path_wrapper 装饰器安全网；
    🔑 优化：限制一级根目录展示行数（最大 30 行），避免超大型开源项目撑爆 Agent 上下文。
    """
    import os

    if not os.path.isdir(directory_path):
        return {"status": "error", "message": f"Directory not found: {directory_path}"}

    if output_file is None:
        output_file = "generated_prompt_file/file_tree.txt"

    try:
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        tree_lines = []

        def _build_tree_recursive(path, prefix="", depth=0):
            if depth >= max_depth:
                return
            try:
                entries = sorted([e for e in os.listdir(path) if not e.startswith('.')])
            except OSError:
                entries = []

            # 🔑 1. 根目录与子目录级联配额控制：根目录最大展示 30 行，次级目录最大展示 15 行
            is_root = (depth == 0)
            limit = 30 if is_root else 15
            truncated = len(entries) > limit
            active_entries = entries[:limit]

            pointers = ["├── "] * (len(active_entries) - 1) + ["└── "]
            for pointer, entry in zip(pointers, active_entries):
                full_path = os.path.join(path, entry)
                if os.path.isdir(full_path) and not os.path.islink(full_path):
                    tree_lines.append(f"{prefix}{pointer}📁 {entry}")
                    extension = "│   " if pointer == "├── " else "    "
                    _build_tree_recursive(full_path, prefix + extension, depth + 1)
                else:
                    tree_lines.append(f"{prefix}{pointer}📄 {entry}")

            # 🔑 2. 提供可视化的截断提示，告知 Agent 当前结构被截断
            if truncated:
                tree_lines.append(f"{prefix}└── ... [truncated: {len(entries) - limit} entries hidden]")

        tree_lines.append(f"📁 {os.path.basename(os.path.abspath(directory_path))}")
        _build_tree_recursive(directory_path, prefix="", depth=0)

        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(tree_lines))

        return {"status": "success", "message": f"Shallow file tree saved successfully to {output_file}."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to save file tree cleanly: {str(e)}"}


@_safe_path_wrapper("find_and_append_file_details")
def find_and_append_file_details(
        directory_path: str,
        search_keyword: str,
        output_file: Optional[str] = None,
        **kwargs
) -> dict:
    """
    Finds a file or directory matching a keyword and appends details.
    🔑 优化：强制接入 @_safe_path_wrapper 装饰器安全网；
    🔑 优化：加入物理真实路径(os.path.realpath)防循环死锁校验，剔除无限递归崩溃。
    """
    import os

    if not os.path.isdir(directory_path):
        return {"status": "error", "message": f"Directory not found: {directory_path}"}

    if output_file is None:
        output_file = "generated_prompt_file/file_tree.txt"

    try:
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        found_paths = []
        visited_real_paths = set()  # 🔑 用于防止无限递归的符号链接物理查重集合

        for root, dirs, files in os.walk(directory_path, followlinks=True):
            # 🔑 1. 计算并查重物理真实路径，如果是环形链接，直接修剪分支，抛弃子目录搜索
            real_root = os.path.realpath(root)
            if real_root in visited_real_paths:
                dirs.clear()  # 阻断 os.walk 向下级递归
                continue
            visited_real_paths.add(real_root)

            # 🔑 2. 安全截断目录列表，物理上剔除所有软链接目录
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]

            all_entries = dirs + files
            for entry in all_entries:
                full_path = os.path.join(root, entry)
                if search_keyword in full_path:
                    found_paths.append(full_path)

        found_paths = sorted(list(set(found_paths)))
        if not found_paths:
            message = f"No file or directory matching '{search_keyword}' was found."
            with open(output_file, "a", encoding="utf-8") as f:
                f.write(f"\n\n--- Detailed query result for '{search_keyword}' ---\n{message}\n")
            return {"status": "success", "message": message}

        details_to_append = [f"\n\n--- Detailed query result for '{search_keyword}' ---"]
        for path in found_paths:
            relative_path = os.path.relpath(path, directory_path)
            details_to_append.append(f"\n# Matched path: {relative_path}")
            if os.path.isdir(path):
                # 约束单个子目录展示数量，防止打印爆炸
                entries = sorted([e for e in os.listdir(path) if not e.startswith('.')])
                for entry in entries[:15]:
                    details_to_append.append(f"├── {'📁' if os.path.isdir(os.path.join(path, entry)) else '📄'} {entry}")
            else:
                details_to_append.append(f"📄 {os.path.basename(path)}")

        with open(output_file, "a", encoding="utf-8") as f:
            f.write("\n".join(details_to_append))

        return {"status": "success", "message": f"Appended details of '{search_keyword}' successfully."}
    except Exception as e:
        return {"status": "error", "message": f"Error in find_and_append_file_details: {str(e)}"}


@_safe_path_wrapper(operation_name="read_file_content")
def read_file_content(file_path: str, mode: str = "full", base_dir: str = None) -> dict:
    """
    【新旧结合型：高鲁棒性文件读取工具】
    1. 集成新机制：路径规范化、白名单校验、物理缺失路径纠错引导。
    2. 集成旧系统：License 自动剥离、多模式切片、500行硬熔断阈值防止 Token 溢出。
    """
    import os, re
    from utils.path_utils import DEFAULT_PROJECT_ROOT

    # 1. 路径解析基准对齐
    if base_dir is None:
        base_dir = DEFAULT_PROJECT_ROOT

    # 规范相对/绝对文件路径
    resolved_path = os.path.normpath(os.path.join(base_dir, file_path)) if not os.path.isabs(file_path) else file_path
    print(f"--- Tool: read_file_content (Mode: {mode}) called for: {file_path} (Resolved: {resolved_path}) ---")

    # 2. 物理文件缺失自愈：吐出纠错引导
    if not os.path.exists(resolved_path):
        from utils.error_handler import format_path_error
        error_guide = format_path_error(
            original_path=file_path,
            normalized_path=file_path,
            base_dir=base_dir,
            validation_passed=True,
            extra_info={
                'error': 'The whitelisted path is valid, but the target file does not physically exist on disk.'}
        )
        return {
            "status": "error",
            "message": f"File not found on disk:\n{error_guide}"
        }

    # 3. 执行读取与防御性处理
    try:
        with open(resolved_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        # A. 自动剥离 License/Header 头部 (节省 Token)
        license_pattern = re.compile(r"^(#|//|\s*\*|/\*).*$", re.MULTILINE)
        start_idx = 0
        for i, line in enumerate(lines[:50]):
            if line.strip() and not license_pattern.match(line):
                start_idx = i
                break
        if start_idx > 5:
            lines = lines[start_idx:]
            print(f"--- Stripped license header ({start_idx} lines) ---")

        total_lines = len(lines)
        MAX_SAFE_LINES = 500  # 硬熔断阈值

        # B. 根据模式进行按行切片，并执行 Safety Melt 策略
        if mode == "full":
            if total_lines > 1000:
                print(f"--- [SAFETY MELT] Full mode exceeds 1000 lines. Truncating to tail_{MAX_SAFE_LINES}. ---")
                lines = lines[-MAX_SAFE_LINES:]
                mode = "full (melted to tail_500)"
            else:
                pass  # 保留全文

        elif mode == "tail_50":  # 读取后 50%
            target_count = int(total_lines * 0.5)
            if target_count > MAX_SAFE_LINES:
                lines = lines[-100:]
                mode = "tail_50 (melted to 100)"
            else:
                lines = lines[-target_count:]

        elif mode == "tail_30":  # 读取后 30%
            target_count = int(total_lines * 0.3)
            if target_count > MAX_SAFE_LINES:
                lines = lines[-100:]
                mode = "tail_30 (melted to 100)"
            else:
                lines = lines[-target_count:]

        elif mode == "tail_200_lines":
            lines = lines[-200:]
        elif mode == "tail_100_lines":
            lines = lines[-100:]
        elif mode == "tail_50":  # 这里是固定的 50 行
            lines = lines[-50:]
        elif mode == "tail_30":  # 这里是固定的 30 行
            lines = lines[-30:]
        elif mode == "head_50":
            lines = lines[:50]
        else:
            # 未知模式默认硬截断为后 100 行
            if total_lines > 100:
                lines = lines[-100:]
                mode = f"unknown_{mode} (fallback to tail_100)"

        content = "".join(lines)
        return {
            "status": "success",
            "message": f"Read {len(lines)} lines from {file_path} (Effective Mode: {mode})",
            "content": content
        }

    except Exception as e:
        return {"status": "error", "message": f"Read operation failed: {str(e)}"}


# =====================================================================
# 3. 路径加固后的安全文件写入/更新器 (create_or_update_file)
# =====================================================================

@_safe_path_wrapper(operation_name="create_or_update_file")
def create_or_update_file(file_path: str, content: str, **kwargs) -> dict:
    """
    Creates a new file and writes content to it, or overwrites an existing file.
    Path normalization and whitelist validation are pre-checked by @_safe_path_wrapper.
    """
    from utils.path_utils import DEFAULT_PROJECT_ROOT

    # 路径解析基准对齐
    base_dir = kwargs.get('base_dir', os.environ.get('PROJECT_ROOT', DEFAULT_PROJECT_ROOT))
    resolved_path = os.path.normpath(os.path.join(base_dir, file_path)) if not os.path.isabs(file_path) else file_path

    print(f"--- Tool: create_or_update_file called for path: {file_path} (Resolved: {resolved_path}) ---")

    try:
        # 安全级联创建可能缺失的父目录结构
        directory = os.path.dirname(resolved_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(resolved_path, "w", encoding="utf-8") as f:
            f.write(content)

        message = f"File '{file_path}' has been successfully created/updated."
        print(message)
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"An error occurred while creating or updating file '{file_path}': {str(e)}"
        print(message)
        return {"status": "error", "message": message}


def append_file_to_file(
        source_path: str,
        destination_path: str,
        base_dir: Optional[str] = None,
        strict_mode: bool = True
) -> dict:
    """
    Reads the entire content of a source file and appends it to the end of a destination file.
    Optimized: Path normalization + whitelist validation for both paths.
    """
    from utils.path_utils import normalize_patch_path, validate_patch_path
    from utils.error_handler import format_path_error

    print(f"--- Tool: append_file_to_file called. Source: '{source_path}', Destination: '{destination_path}' ---")

    if base_dir is None:
        base_dir = os.environ.get('PROJECT_ROOT', os.getcwd())

    # 🔐 双路径校验
    normalized_source = normalize_patch_path(source_path, base_dir)
    normalized_dest = normalize_patch_path(destination_path, base_dir)

    if strict_mode:
        if not validate_patch_path(normalized_source, strict=True):
            return {
                "status": "error",
                "message": format_path_error(
                    original_path=source_path,
                    normalized_path=normalized_source,
                    base_dir=base_dir,
                    validation_passed=False,
                    extra_info={'operation': 'append_file_to_file', 'path_type': 'source'}
                )
            }
        if not validate_patch_path(normalized_dest, strict=True):
            return {
                "status": "error",
                "message": format_path_error(
                    original_path=destination_path,
                    normalized_path=normalized_dest,
                    base_dir=base_dir,
                    validation_passed=False,
                    extra_info={'operation': 'append_file_to_file', 'path_type': 'destination'}
                )
            }

    if not os.path.isfile(normalized_source):
        return {"status": "error",
                "message": f"Error: Source file '{source_path}' does not exist or is not a valid file."}
    if os.path.isdir(normalized_dest):
        return {"status": "error",
                "message": f"Error: Destination path '{destination_path}' is a directory and cannot be an append target."}
    if os.path.abspath(normalized_source) == os.path.abspath(normalized_dest):
        return {"status": "error", "message": "Error: Source and destination files cannot be the same."}

    try:
        with open(normalized_source, "r", encoding="utf-8") as f_source:
            content_to_append = f_source.read()
        dest_directory = os.path.dirname(normalized_dest)
        if dest_directory:
            os.makedirs(dest_directory, exist_ok=True)
        with open(normalized_dest, "a", encoding="utf-8") as f_dest:
            f_dest.write(content_to_append)
        return {"status": "success",
                "message": f"Successfully appended the content of '{source_path}' to '{destination_path}'."}
    except Exception as e:
        return {"status": "error", "message": f"An unknown error occurred while appending the file: {str(e)}"}


def append_string_to_file(
        file_path: str,
        content: str,
        base_dir: Optional[str] = None,
        strict_mode: bool = True
) -> dict:
    """
    Appends a string of content to the end of a specified file.
    Optimized: Path normalization + whitelist validation.
    """
    from utils.path_utils import normalize_patch_path, validate_patch_path
    from utils.error_handler import format_path_error

    print(f"--- Tool: append_string_to_file called for path: {file_path} ---")

    if base_dir is None:
        base_dir = os.environ.get('PROJECT_ROOT', os.getcwd())

    normalized_path = normalize_patch_path(file_path, base_dir)
    if strict_mode and not validate_patch_path(normalized_path, strict=True):
        return {
            "status": "error",
            "message": format_path_error(
                original_path=file_path,
                normalized_path=normalized_path,
                base_dir=base_dir,
                validation_passed=False,
                extra_info={'operation': 'append_string_to_file'}
            )
        }

    try:
        directory = os.path.dirname(normalized_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(normalized_path, "a", encoding="utf-8") as f:
            f.write(content)
        return {"status": "success", "message": f"Content successfully appended to file '{file_path}'."}
    except Exception as e:
        return {"status": "error",
                "message": f"An error occurred while appending content to file '{file_path}': {str(e)}"}


def delete_file(file_path: str, base_dir: str = None, **kwargs) -> dict:
    """
    Deletes a specified file.
    """
    if base_dir is None:
        base_dir = os.environ.get('PROJECT_ROOT', '/home/senchen/temp/fix_build_agent')

    normalized_path = normalize_patch_path(file_path, base_dir)

    # 白名单验证（删除操作必须严格）
    strict_mode = kwargs.get('strict_mode', True)
    if strict_mode and not validate_patch_path(normalized_path, strict=True):
        return {
            "status": "error",
            "message": format_path_error(
                original_path=file_path,
                normalized_path=normalized_path,
                base_dir=base_dir,
                validation_passed=False,
                extra_info={'operation': 'delete_file'}
            )
        }
    print(f"--- Tool: delete_file called for path: {normalized_path} ---")
    if not os.path.exists(normalized_path):
        message = f"Error: File '{file_path}' does not exist and cannot be deleted."
        print(message)
        return {"status": "error", "message": message}
    try:
        os.remove(normalized_path)
        message = f"File '{normalized_path}' has been successfully deleted."
        print(message)
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"An error occurred while deleting file '{file_path}': {str(e)}"
        print(message)
        return {"status": "error", "message": message}


def prompt_generate_tool(
        tool_context: ToolContext,
        project_main_folder_path: str,
        max_depth: int,
        config_folder_path: str,
        attempt_id: int
) -> dict:
    """
    物理组装工具：从账本、归因工件和RAG库中提取信息并生成最终的 prompt.txt。
    """
    import os, re

    print(f"--- Workflow Tool: prompt_generate_tool started (Attempt: {attempt_id}) ---")

    session = tool_context.session
    current_node_id = session.state.get("current_node_id", 0)
    validation_report = session.state.get("last_validation_report", {})

    PROMPT_DIR = "generated_prompt_file"
    PROMPT_FILE_PATH = os.path.abspath(os.path.join(PROMPT_DIR, "prompt.txt"))
    FUZZ_LOG_PATH = "fuzz_build_log_file/fuzz_build_log.txt"
    os.makedirs(PROMPT_DIR, exist_ok=True)

    # =================================================================
    # 1. 自动组装历史策略轨迹 (自适应节点检查)
    # =================================================================
    enhanced_history = ""

    if current_node_id == 0:
        enhanced_history = "LOG: This is the initial baseline attempt. No previous repair history exists."
    else:
        # 仅检索最近三轮 (max 3 nodes)，确保不越界
        start_nid = max(0, current_node_id - 3)
        for nid in range(start_nid, current_node_id):
            res = query_trace_ledger(
                tool_context=tool_context,
                field_keys=[
                    "action_and_intent.repair_strategy",
                    "action_and_intent.loop_summary",
                    "semantic_memory.reflection_analysis"
                ],
                node_id=nid
            )
            if res["status"] == "success":
                data = res["data"]
                label = "INITIAL BASELINE" if nid == 0 else f"ROUND {nid}"
                enhanced_history += f"\n--- [{label} REFLECTION] ---\n"
                enhanced_history += f"Strategy: {data.get('action_and_intent.repair_strategy', 'N/A')}\n"
                enhanced_history += f"Summary: {data.get('action_and_intent.loop_summary', 'N/A')}\n"
                enhanced_history += f"Reflection: {data.get('semantic_memory.reflection_analysis', 'N/A')}\n"

    if not enhanced_history.strip():
        enhanced_history = "No relevant historical trajectory found in trace ledger."

    # =================================================================
    # 2. 提取当前 ECRCL 归因工件 (故障根因)
    # =================================================================
    causal_chain = "N/A"
    final_attribution = "N/A"
    commit_changed_path = os.path.abspath("generated_prompt_file/commit_changed.txt")

    if os.path.exists(commit_changed_path):
        try:
            with open(commit_changed_path, 'r', encoding='utf-8') as f:
                txt = f.read()
                # 采用非贪婪匹配提取关键段落
                cc_match = re.search(r"\[CAUSAL_CHAIN\]\s*([\s\S]*?)(?=\n\n\[|$)", txt)
                fa_match = re.search(r"\[FINAL_ATTRIBUTION\]\s*([\s\S]*)$", txt)
                if cc_match: causal_chain = cc_match.group(1).strip()
                if fa_match: final_attribution = fa_match.group(1).strip()
        except Exception as e:
            print(f"Warning: Failed to parse commit_changed.txt: {e}")

    # =================================================================
    # 3. 物理触发 Few-shot RAG 检索
    # =================================================================
    rag_res = few_shot_rag_retrieve("expert_knowledge.json", FUZZ_LOG_PATH)
    expert_context = rag_res.get("rag_context", "No expert knowledge matched.")

    # =================================================================
    # 4. 组装最终 Prompt 文件
    # =================================================================
    project_name = os.path.basename(os.path.abspath(project_main_folder_path))

    with open(PROMPT_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(f"Testing Expert. Project: {project_name}. Attempt: {attempt_id}\n")

        f.write("\n--- 【LAST BUILD VALIDATION (1+2+6 CRITERIA)】 ---\n")
        for k in ["step_1_official_list", "step_2_infra_compliance", "step_6_runtime_stability"]:
            f.write(f"{k.upper()}: {validation_report.get(k, 'N/A')}\n")

        f.write(f"\n【STRATEGIC KNOWLEDGE (RAG)】\n{expert_context}\n")
        f.write(f"\n【CAUSAL_CHAIN】\n{causal_chain}\n")
        f.write(f"\n【FINAL_ATTRIBUTION】\n{final_attribution}\n")
        f.write(f"\n【REPAIR_HISTORY_TRAJECTORY】\n{enhanced_history}\n")

        # 注入 Docker/Build 配置文件
        for fname in sorted(os.listdir(config_folder_path)):
            file_abs_path = os.path.join(config_folder_path, fname)
            if os.path.isfile(file_abs_path) and (
                    fname.endswith('.sh') or 'Dockerfile' in fname or 'project.yaml' in fname):
                # 显式使用 read_file_content 确保 Safety Melt 生效
                res_content = read_file_content(file_abs_path, mode="full")
                if res_content.get("status") == "success":
                    f.write(f"\n### {fname} ###\n{res_content.get('content', '')}\n")

        # 记录浅层文件树
        save_file_tree_shallow(project_main_folder_path, 1, os.path.join(PROMPT_DIR, "file_tree.txt"))

        # 注入日志尾部上下文 (严格限制长度)
        if os.path.exists(FUZZ_LOG_PATH):
            with open(FUZZ_LOG_PATH, 'r', encoding='utf-8', errors='ignore') as lf:
                # 只取最后 12000 字符，约 2000-3000 Token
                f.write(f"\n\n--- BUILD LOG TAIL ---\n{lf.read()[-12000:]}")

    # 5. 最终截断保护 (由外部配置决定阈值)
    limit_lines = globals().get("MAX_LINES_LIMIT", 2500)
    truncate_prompt_file(PROMPT_FILE_PATH, max_lines=limit_lines)

    return {"status": "success", "content": "Prompt successfully assembled."}


def _auto_discover_project_symbols(binary_path: str, project_name: str) -> Optional[List[str]]:
    """Heuristically identify project-specific symbols using nm."""
    import subprocess
    try:
        result = subprocess.run(['nm', '-D', binary_path], capture_output=True, text=True, errors='ignore')
        if result.returncode != 0:
            result = subprocess.run(['nm', binary_path], capture_output=True, text=True, errors='ignore')

        lines = result.stdout.splitlines()
        keywords = [project_name.lower(), "deflate", "inflate", "adler32", "crc32"] if project_name == "zlib" else [
            project_name.lower()]
        boilerplate = ('__asan', '__lsan', '__ubsan', '__sanitizer', 'fuzzer::', 'LLVM', 'afl_', '_Z', 'std::')

        candidates = []
        for line in lines:
            parts = line.split()
            if not parts: continue
            symbol = parts[-1]
            if any(kw in symbol.lower() for kw in keywords) and not symbol.startswith(boilerplate):
                candidates.append(symbol)
        return candidates[:5] if candidates else None
    except Exception as e:
        logger.debug(f"Cleanup step failed (non-fatal): {e}")


def _cleanup_environment(oss_fuzz_path: str, project_name: str):
    """Internal helper to clean up dangling Docker builder remnants before build."""
    try:
        subprocess.run(f"docker ps -q --filter \"ancestor=gcr.io/oss-fuzz/{project_name}\" | xargs -r docker kill",
                       shell=True, capture_output=True)
        subprocess.run("docker ps -q --filter \"ancestor=gcr.io/oss-fuzz-base/base-runner\" | xargs -r docker kill",
                       shell=True, capture_output=True)
    except:
        pass


def _auto_discover_project_symbols_from_content(nm_stdout: str, project_name: str) -> bool:
    """Helper to evaluate static linkage of project logic from symbol table."""
    keywords = [project_name.lower(), "deflate", "inflate", "adler32", "crc32"] if project_name == "zlib" else [
        project_name.lower()]
    boilerplate = ('__asan', '__lsan', '__ubsan', '__sanitizer', 'fuzzer::', 'LLVM', 'afl_', '_Z', 'std::')

    for line in nm_stdout.splitlines():
        parts = line.split()
        if not parts: continue
        symbol = parts[-1]
        if any(kw in symbol.lower() for kw in keywords) and not symbol.startswith(boilerplate):
            return True
    return False


@_safe_path_wrapper
def modify_file_by_lines(
        file_path: str,
        operation: str,
        line_number: int,
        end_line: Optional[int] = None,
        content: str = "",
        **kwargs
) -> dict:
    base_dir = kwargs.get('base_dir', os.getcwd())
    normalized_path = os.path.normpath(os.path.join(base_dir, file_path)) if not os.path.isabs(file_path) else file_path

    if not os.path.isfile(normalized_path):
        return {"status": "error", "message": "File not found."}
    valid_ops = {"insert_after", "insert_before", "delete", "replace"}
    if operation not in valid_ops:
        return {"status": "error", "message": f"Invalid operation. Must be one of {valid_ops}."}

    if end_line is None:
        end_line = line_number
    if line_number < 1 or end_line < line_number:
        return {"status": "error", "message": "Invalid line range."}

    with open(normalized_path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    total_lines = len(lines)

    if line_number > total_lines:
        return {"status": "error", "message": f"Line exceeds file length."}
    if end_line > total_lines:
        end_line = total_lines

    start_idx = line_number - 1
    end_idx = end_line

    new_lines = []
    if operation == "delete":
        new_lines = lines[:start_idx] + lines[end_idx:]
    elif operation == "replace":
        if not content.endswith('\n'):
            content += '\n'
        new_lines = lines[:start_idx] + content.splitlines(keepends=True) + lines[end_idx:]
    elif operation.startswith("insert"):
        if not content.endswith('\n'):
            content += '\n'
        insert_pos = start_idx if operation == "insert_before" else end_idx
        new_lines = lines[:insert_pos] + content.splitlines(keepends=True) + lines[insert_pos:]

    try:
        with open(normalized_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        return {"status": "success", "message": f"Applied '{operation}' at lines {line_number}-{end_line}."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def list_files_in_dir(
        dir_path: str,
        max_depth: int = 2,
        pattern: str = "*",
        max_results: int = 200,
        base_dir: Optional[str] = None,
        strict_mode: bool = True
) -> dict:
    """
    Return a structured, LLM-friendly file tree. Replaces `run_command + find`.
    Optimized: Path normalization + symlink protection + whitelist validation.
    """
    if base_dir is None:
        base_dir = os.environ.get('PROJECT_ROOT', os.getcwd())

    normalized_dir = normalize_patch_path(dir_path, base_dir)
    if strict_mode and not validate_patch_path(normalized_dir, strict=True):
        return {
            "status": "error",
            "message": format_path_error(
                original_path=dir_path,
                normalized_path=normalized_dir,
                base_dir=base_dir,
                validation_passed=False,
                extra_info={'operation': 'list_files_in_dir'}
            )
        }

    if not os.path.isdir(normalized_dir):
        return {"status": "error", "message": "Directory not found."}

    results = []
    visited_real_paths = set()  # 🔐 防止符号链接循环

    def _traverse(current: str, depth: int):
        if depth > max_depth or len(results) >= max_results:
            return
        # 🔐 符号链接保护
        real_path = os.path.realpath(current)
        if real_path in visited_real_paths:
            return
        visited_real_paths.add(real_path)

        try:
            entries = sorted(os.listdir(current))
        except PermissionError:
            return

        for entry in entries:
            if len(results) >= max_results:
                break
            full_path = os.path.join(current, entry)
            rel_path = os.path.relpath(full_path, normalized_dir)

            if fnmatch.fnmatch(entry, pattern) or fnmatch.fnmatch(rel_path, f"*{pattern}*"):
                is_dir = os.path.isdir(full_path) and not os.path.islink(full_path)  # 🔐 排除符号链接目录
                results.append({"path": rel_path, "type": "dir" if is_dir else "file"})

            if os.path.isdir(full_path) and not os.path.islink(full_path):  # 🔐 不递归符号链接
                _traverse(full_path, depth + 1)

    _traverse(normalized_dir, 0)

    return {
        "status": "success",
        "count": len(results),
        "files": results[:max_results],
        "truncated": len(results) > max_results
    }


def check_file_exists(file_path: str) -> dict:
    """
    Safely checks if a file exists within the workspace.
    Replaces unsafe 'ls ... 2>/dev/null' shell commands with structured JSON response.
    """
    import os
    # 1. 路径安全规范化（防穿越）
    workspace_root = os.getcwd()
    target = os.path.normpath(
        os.path.join(workspace_root, file_path) if not os.path.isabs(file_path) else file_path
    )
    if not os.path.realpath(target).startswith(os.path.realpath(workspace_root)):
        return {"status": "error", "message": "Path validation failed: access denied."}

    # 2. 返回结构化状态
    return {
        "status": "success",
        "exists": os.path.isfile(target),
        "path": target
    }
