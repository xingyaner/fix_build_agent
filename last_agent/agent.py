import os
from google.adk.agents import LoopAgent, LlmAgent, BaseAgent, SequentialAgent
from google.adk.tools.tool_context import ToolContext

# 假设这些工具函数在您的项目中是可用的
# 如果它们定义在别的文件中，请确保 Python 路径正确
from agent_tools import (
    run_fuzz_build_streaming,
    read_file_content,
    prompt_generate_tool,
    create_or_update_file,
    apply_solution_file
)

# --- 常量和状态键 ---
# 这些常量在 adk run 模式下主要用于代码内部逻辑
APP_NAME = "fuzz_fixer_app"
MODEL_NAME = "gemini-1.5-pro-latest" # 使用一个统一的、具体的模型名称

# State keys to pass data between agents
STATE_PROJECT_NAME = "project_name"
STATE_PROJECT_CONFIG_PATH = "project_config_path"
STATE_PROJECT_SOURCE_PATH = "project_source_path"
STATE_FUZZ_LOG_CONTENT = "fuzz_log_content"

# File path constants
FUZZ_LOG_PATH = "fuzz_build_log_file/fuzz_build_log.txt"
PROMPT_PATH = "generated_prompt_file/prompt.txt"
SOLUTION_PATH = "solution.txt"

# --- 工具定义 ---
# 所有工具函数都需要在此文件中定义或导入，以便 `adk run` 能够发现它们。

def exit_loop(tool_context: ToolContext):
  """当fuzz构建成功时调用此函数，以终止修复循环。"""
  print(f"  [Tool Call] exit_loop triggered by {tool_context.agent_name}")
  tool_context.actions.escalate = True
  return {"message": "Loop exit signal sent."}

def save_initial_info(
    tool_context: ToolContext,
    project_name: str,
    project_config_path: str,
    project_source_path: str
):
    """保存用户提供的项目初始信息到会话状态中。"""
    print(f"  [Tool Call] Saving initial info to state: {project_name}, {project_config_path}, {project_source_path}")
    tool_context.state[STATE_PROJECT_NAME] = project_name
    tool_context.state[STATE_PROJECT_CONFIG_PATH] = project_config_path
    tool_context.state[STATE_PROJECT_SOURCE_PATH] = project_source_path
    return {"status": "Information saved successfully."}

# --- Agent 定义 ---

# 步骤 1: 初始信息收集 Agent
initial_info_collector_agent = LlmAgent(
    name="InitialInfoCollector",
    model=MODEL_NAME,
    instruction="""你的任务是从用户的对话中解析出三个关键信息：项目名称、项目配置文件路径和项目文件所在路径。
一旦你获得了这三个信息，必须立即调用 `save_initial_info` 工具将它们保存起来。""",
    description="从用户处获取并保存初始项目配置。",
    tools=[save_initial_info]
)

# --- 循环内部的 Agents ---

# 步骤 2a: 运行Fuzz并记录Log
run_fuzz_agent_in_loop = LlmAgent(
    name="RunFuzzAgent",
    model=MODEL_NAME,
    include_contents='none',
    instruction="""你的任务是执行Fuzzing构建。
    必须调用 `run_fuzz_build_streaming` 工具。
    `project_name` 参数的值是 `{{project_name}}`。
    `oss_fuzz_path` 参数的值是 `{{project_config_path}}` 的父目录 (例如, 如果路径是 '/root/oss-fuzz/projects/aiohttp', 则使用 '/root/oss-fuzz/projects')。
    """,
    description="运行fuzz构建命令并记录日志。",
    tools=[run_fuzz_build_streaming],
)

# 步骤 2b: 读取Fuzz构建日志
log_reader_agent = LlmAgent(
    name="LogReaderAgent",
    model=MODEL_NAME,
    include_contents='none',
    instruction=f"你的任务是读取Fuzz构建的日志文件。日志文件位于 '{FUZZ_LOG_PATH}'。请使用 `read_file_content` 工具来读取它。",
    description="读取fuzz构建日志文件的内容。",
    tools=[read_file_content],
    output_key=STATE_FUZZ_LOG_CONTENT
)

# 步骤 2c: 检查是否成功，若成功则退出循环
success_check_agent = LlmAgent(
    name="SuccessCheckAgent",
    model=MODEL_NAME,
    include_contents='none',
    instruction=f"""你是一个流程控制器。你需要分析日志内容。
    **日志内容:**
    ```
    {{{{fuzz_log_content}}}}
    ```
    如果日志内容**完全**是 "success"，你**必须**调用 `exit_loop` 函数来终止流程。
    否则，不要调用任何工具，也不要产生任何输出。
    """,
    description="检查fuzz构建是否成功，如果成功则发送退出循环信号。",
    tools=[exit_loop],
)

# 步骤 2d: 生成Prompt
prompt_generate_agent_in_loop = LlmAgent(
    name="PromptGenerateAgent",
    model=MODEL_NAME,
    include_contents='none',
    instruction=f"""你的任务是为解决Fuzzing报错准备信息。
    必须调用 `prompt_generate_tool` 工具。
    该工具需要以下三个路径参数：
    1. `tree_path`: `{{{{project_source_path}}}}`
    2. `config_path`: `{{{{project_config_path}}}}`
    3. `log_path`: `{FUZZ_LOG_PATH}`
    """,
    description="搜集报错日志、文件树和配置文件内容以生成prompt。",
    tools=[prompt_generate_tool],
)

# 步骤 2e: 生成解决方案
fuzzing_solver_agent_in_loop = LlmAgent(
    name="FuzzingSolverAgent",
    model=MODEL_NAME,
    include_contents='none',
    instruction=f"""你是一位世界顶级的软件测试专家，任务是生成解决方案。
    1. 首先，使用 `read_file_content` 工具读取位于 `{PROMPT_PATH}` 的prompt文件来理解问题。
    2. 思考并生成一个解决方案。解决方案必须严格遵循格式要求（使用 `---=== FILE ===---` 作为分隔符）。
    3. 最后，调用 `create_or_update_file` 工具，将你生成的完整解决方案文本保存到 `{SOLUTION_PATH}` 文件中。
    """,
    description="读取prompt，生成解决方案。",
    tools=[read_file_content, create_or_update_file],
)

# 步骤 2f: 应用解决方案
solution_applier_agent_in_loop = LlmAgent(
    name="SolutionApplierAgent",
    model=MODEL_NAME,
    include_contents='none',
    instruction=f"""你的任务是应用已生成的解决方案。
    必须调用 `apply_solution_file` 工具。
    `solution_file_path` 参数的值是 `{SOLUTION_PATH}`。
    `target_directory` 参数的值是 `{{{{project_config_path}}}}`。
    """,
    description="应用解决方案，修改项目文件。",
    tools=[apply_solution_file],
)


# --- 步骤 2: 组装循环智能体 ---
workflow_loop_agent = LoopAgent(
    name="FuzzFixLoop",
    sub_agents=[
        run_fuzz_agent_in_loop,
        log_reader_agent,
        success_check_agent,
        prompt_generate_agent_in_loop,
        fuzzing_solver_agent_in_loop,
        solution_applier_agent_in_loop,
    ],
    max_iterations=5 # 设置最大循环次数以防止无限循环
)

# 步骤 3: 最终成功响应 Agent
final_responder_agent = LlmAgent(
    name="FinalResponder",
    model=MODEL_NAME,
    include_contents="none",
    instruction="整个Fuzzing问题的自动修复流程已经成功完成。请向用户报告这个好消息，并告知问题已被解决。",
    description="在循环成功结束后向用户报告。"
)


# --- 根 Agent: 整体流程编排 ---
# 为了与 `adk run` 兼容，根智能体必须命名为 `root_agent`
root_agent = SequentialAgent(
    name="FuzzFixWorkflowManager",
    sub_agents=[
        initial_info_collector_agent,
        workflow_loop_agent,
        final_responder_agent
    ],
    description="一个完整的自动化Fuzzing问题修复工作流。"
)
