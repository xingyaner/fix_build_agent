from google.adk.agents import Agent
from agent_tools import read_file_content, create_or_update_file
from .util import load_instruction_from_file

# --- Fuzzing 问题解决 Agent ---
root_agent = Agent(
    name="fuzzing_solver_agent",
    model="gemini-2.5-pro",
    instruction=load_instruction_from_file("fuzzing_solver_instruction.txt"),
    description="一个能够分析fuzzing上下文、生成解决方案并将其保存到文件中的专家代理。",
    # 唯一的“行动”就是读取上下文文件。
    tools=[read_file_content, create_or_update_file],
    output_key="solution_plan",  # 把结果存入state
)
