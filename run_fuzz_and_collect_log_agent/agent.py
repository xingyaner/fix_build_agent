from google.adk.agents import Agent
from agent_tools import run_fuzz_build, run_fuzz_build_streaming, create_or_update_file
from .util import load_instruction_from_file

# 定义一个专门用于Fuzzing的编排Agent
root_agent = Agent(
    name="run_fuzz_and_collect_log_agent",
    model="gemini-2.5-pro",
    instruction=load_instruction_from_file("run_fuzz_and_collect_log_instruction.txt"),
    description="一个能够执行Fuzzing构建命令、捕获错误并自动保存错误日志并实时显示进度的的高级代理。",
    tools=[run_fuzz_build_streaming, create_or_update_file],
    output_key="fuzz_build_log",  # 把结果存入state
)
