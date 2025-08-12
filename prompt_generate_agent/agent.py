# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Shows how to call all the sub-agents using the LLM's reasoning ability. Run this with "adk run" or "adk web"

from google.adk.agents import LlmAgent
from google.adk.tools import google_search

from .util import load_instruction_from_file


# --- Sub Agent 1: prompt generate ---
prompt_generate_agent = LlmAgent(
    name="prompt_generate_agent",
    model="gemini-2.5-pro",
    instruction=load_instruction_from_file("prompt_generate_instruction.txt"),
    description="你是prompt生成者,一位资深的书写prompt的专家,专门为Gemini-2.5-pro书写简明有效的prompt",
    output_key="generated_prompt", # 把结果存入state

)

# --- Sub Agent 2: subject ---
subject_agent = LlmAgent(
    name="subject_agent",
    model="gemini-2.5-pro",
    instruction=load_instruction_from_file("subject_agent_instruction.txt"),
    description="你是软件测试方面首屈一指的专家，尤其擅长fuzz编译和构建问题的解决",
    output_key="solution_plan",  # 把结果存入state
)

# --- Sub Agent 3: content modification ---
content_modification_agent = LlmAgent(
    name="content_modification_agent",
    model="gemini-2.5-pro",
    instruction=load_instruction_from_file("content_modification_instruction.txt"),
    description="配置文件修改者，负责根据subject_agent生成的文件修改代码对配置文件进行修改",
    output_key="file_modificate",  # 把结果存入state
)

# --- Sub Agent 4: run fuzz and collect log ---
run_fuzz_and_collect_log_agent = LlmAgent(
    name="run_fuzz_and_collect_log_agent",
    model="gemini-2.5-pro",
    instruction=load_instruction_from_file("run_fuzz_and_collect_log_instruction.txt"),
    description="负责运行项目的编译构建过程并判断、采集运行结果的log，将信息提供给prompt_generate_agent",
    output_key="fuzz_build_log",  # 把结果存入state
)

# --- Loop Agent Workflow ---
workflow_loop_agent = LoopAgent(
    name="workflow_loop_agent",
    sub_agents=[prompt_generate_agent, subject_agent, formatter_agent,content_modification_agent,run_fuzz_and_collect_log_agent],
)

root_agent = workflow_loop_agent
