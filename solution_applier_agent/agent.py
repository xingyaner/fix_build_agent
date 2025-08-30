from google.adk.agents import Agent
from agent_tools import apply_solution_file
from .util import load_instruction_from_file

root_agent = Agent(
    name="solution_applier_agent",
    model="gemini-2.5-pro",
    instruction=(
        "你的任务是执行一个文件修改任务。你需要从用户那里获取两个信息："
        "1. `solution_file_path`: 包含了修改方案的文件的路径。"
        "2. `target_directory`: 需要应用这些修改的项目根目录的路径。"
        "获取到这两个信息后，你必须调用 `apply_solution_file` 工具来完成任务，然后向用户报告执行结果。"
    ),
    description="一个能够读取解决方案文件并将其应用到目标项目中的执行代理。",
    tools=[apply_solution_file],
    output_key="solution_applier",  # 把结果存入state
)
